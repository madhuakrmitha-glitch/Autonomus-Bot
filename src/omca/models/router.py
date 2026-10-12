"""Metered model router: every application-controlled model call goes through ``call``.

Responsibilities: role -> allowlisted route chain, modality checks, image preparation with
recorded resize transforms, conservative budget reservation, provider call, usage
settlement, bounded retries and fallbacks. Retries and helper calls are metered the same
way as primary calls.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any

from ..budget import (
    BudgetBlocked,
    BudgetLedger,
    CallEstimate,
    Usage,
    estimate_image_tokens,
    estimate_text_tokens,
)
from ..config import ApplicationConfig, ModelRoute
from ..contracts import ErrorCode
from .providers import FixtureProvider, OpenAICompatibleProvider, Provider, ProviderError
from .registry import ModelRegistry, RouteRejected
from .types import (
    ImageInput,
    ImageTransform,
    ModelResponse,
    ToolCall,
    count_images,
    image_part,
    strip_thinking,
    text_of,
)


class ModelCallError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


def prepare_image(img: ImageInput, max_side: int) -> tuple[ImageInput, ImageTransform]:
    """Downscale (never upscale) to the route's limit, keeping the aspect ratio; record the transform."""
    from PIL import Image

    with Image.open(io.BytesIO(img.data)) as im:
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
            resized = im.convert("RGB").resize((nw, nh), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, "PNG")
            out = ImageInput(buf.getvalue(), "image/png", img.artifact_id, img.label)
        else:
            nw, nh = w, h
            out = img
    return out, ImageTransform(img.artifact_id, w, h, nw, nh)


class ModelRouter:
    def __init__(self, config: ApplicationConfig, registry: ModelRegistry, ledger: BudgetLedger,
                 providers: dict[str, Provider] | None = None):
        self.config = config
        self.registry = registry
        self.ledger = ledger
        compat = OpenAICompatibleProvider()
        self.providers: dict[str, Provider] = providers or {
            "openai_compatible": compat,
            "ollama": compat,
            "hf_inference_providers": compat,
            "hf_dedicated_endpoint": compat,
            "fixture": FixtureProvider(),
        }

    def provider(self, route: ModelRoute) -> Provider:
        return self.providers[route.provider]

    # -- public API ---------------------------------------------------------------------
    async def call(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        images: list[ImageInput] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        max_output_tokens: int | None = None,
        temperature: float | None = 0.2,
        task_id: str | None = None,
        task_ceiling: float | None = None,
        purpose: str | None = None,
        caller: str = "coordinator",
        response_format: dict[str, Any] | None = None,
    ) -> ModelResponse:
        images = images or []
        needs = {"text"}
        if images or count_images(messages):
            needs.add("image")
        if tools:
            needs.add("tools")
        try:
            chain = self.registry.chain(role, needs)
        except RouteRejected as exc:
            raise ModelCallError(ErrorCode.ROUTE_REJECTED, str(exc)) from exc
        if tools:
            chain = [r for r in chain if r.supports_tools]
            if not chain:
                raise ModelCallError(ErrorCode.UNSUPPORTED_CAPABILITY, f"no route for {role} supports tool calls")
        last_error: Exception | None = None
        for route in chain:
            try:
                return await self._call_route(route, messages, images, tools, tool_choice, max_output_tokens,
                                              temperature, task_id, task_ceiling, purpose or role, caller,
                                              response_format)
            except BudgetBlocked:
                raise
            except ProviderError as exc:
                last_error = exc
                continue
        raise ModelCallError(ErrorCode.INTERNAL, f"all routes for {role} failed: {last_error}")

    async def call_route(self, route_id: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        """Direct route call used by probes and the OpenAI-compatible gateway (still allowlisted and metered)."""
        needs = {"text"} | ({"image"} if kw.get("images") or count_images(messages) else set())
        allow_unprobed = kw.pop("allow_unprobed", False)
        if allow_unprobed:
            route = self.registry.get(route_id)
            problem = self.registry.open_model_problem(route) or (None if route.enabled else "route is disabled")
            problem = problem or self.ledger.route_billing_problem(route)
            if problem:
                raise ModelCallError(ErrorCode.ROUTE_REJECTED, f"route {route_id} rejected: {problem}")
        else:
            try:
                route = self.registry.require_usable(route_id, needs)
            except RouteRejected as exc:
                raise ModelCallError(ErrorCode.ROUTE_REJECTED, str(exc)) from exc
        return await self._call_route(route, messages, kw.get("images") or [], kw.get("tools"), kw.get("tool_choice"),
                                      kw.get("max_output_tokens"), kw.get("temperature", 0.2), kw.get("task_id"),
                                      kw.get("task_ceiling"), kw.get("purpose", "direct"), kw.get("caller", "coordinator"),
                                      kw.get("response_format"))

    # -- internals -----------------------------------------------------------------------
    def build_body(self, route: ModelRoute, messages: list[dict[str, Any]], images: list[ImageInput],
                   tools: list[dict[str, Any]] | None, tool_choice: Any, max_tokens: int,
                   temperature: float | None, response_format: dict[str, Any] | None
                   ) -> tuple[dict[str, Any], list[ImageTransform], int]:
        msgs = [dict(m) for m in messages]
        transforms: list[ImageTransform] = []
        image_tokens = 0
        if images:
            prepared = []
            for img in images:
                p, t = prepare_image(img, route.max_image_side)
                prepared.append(p)
                transforms.append(t)
                image_tokens += estimate_image_tokens(t.sent_width, t.sent_height, route.image_patch_px, route.max_image_side)
            # attach images to the last user message
            idx = max(i for i, m in enumerate(msgs) if m.get("role") == "user")
            content = msgs[idx].get("content")
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
            for p in prepared:
                if p.label:
                    parts.append({"type": "text", "text": p.label})
                parts.append(image_part(p))
            msgs[idx] = {**msgs[idx], "content": parts}
        body: dict[str, Any] = {"model": route.model, "messages": msgs, "max_tokens": max_tokens, "stream": False}
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if response_format:
            body["response_format"] = response_format
        body.update(route.extra_body)
        return body, transforms, image_tokens

    async def _call_route(self, route, messages, images, tools, tool_choice, max_output_tokens, temperature, task_id,
                          task_ceiling, purpose, caller, response_format) -> ModelResponse:
        max_tokens = min(max_output_tokens or route.max_output_tokens, route.max_output_tokens)
        body, transforms, image_tokens = self.build_body(route, messages, images, tools, tool_choice, max_tokens,
                                                         temperature, response_format)
        n_images = len(count_images(body["messages"]))
        text_tokens = estimate_text_tokens(text_of(body["messages"])) + (estimate_text_tokens(str(tools)) if tools else 0)
        if n_images and not image_tokens:
            image_tokens = n_images * estimate_image_tokens(route.max_image_side, route.max_image_side,
                                                            route.image_patch_px, route.max_image_side)
        if text_tokens + image_tokens + max_tokens > route.context_limit:
            raise ModelCallError(ErrorCode.LIMIT_EXCEEDED,
                                 f"request needs ~{text_tokens + image_tokens + max_tokens} tokens; "
                                 f"route {route.id} context is {route.context_limit}")
        est = CallEstimate(input_tokens=text_tokens, images=n_images, image_tokens=image_tokens,
                           max_output_tokens=max_tokens + route.output_overrun_tokens)
        attempts = self.config.limits.max_retries_per_action + 1
        last: ProviderError | None = None
        for attempt in range(1, attempts + 1):
            call_id = self.ledger.reserve(route, est, task_id=task_id, purpose=purpose, caller=caller,
                                          task_ceiling=task_ceiling, attempt=attempt)
            started = time.monotonic()
            try:
                raw = await self.provider(route).chat(route, body)
            except ProviderError as exc:
                if exc.billed_possible:
                    self.ledger.mark_unknown(call_id, str(exc))
                else:
                    self.ledger.release(call_id, str(exc))
                last = exc
                if exc.retryable and attempt < attempts:
                    await asyncio.sleep(min(8.0, 0.5 * 2 ** attempt) if not self.config.testing else 0)
                    continue
                raise
            except asyncio.CancelledError:
                self.ledger.mark_unknown(call_id, "cancelled while the request was in flight")
                raise
            latency = int((time.monotonic() - started) * 1000)
            usage = raw.get("usage") if isinstance(raw, dict) else None
            u = None
            if usage and route.usage_reporting:
                u = Usage(usage.get("prompt_tokens"), usage.get("completion_tokens"), None, "provider")
            self.ledger.settle(call_id, route, u, images=n_images, latency_ms=latency)
            return self._parse(route, raw, call_id, latency, transforms)
        assert last is not None
        raise last

    @staticmethod
    def _parse(route: ModelRoute, raw: dict[str, Any], call_id: str, latency: int,
               transforms: list[ImageTransform]) -> ModelResponse:
        import json

        try:
            choice = raw["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{route.id} returned no choices", retryable=False, billed_possible=True) from exc
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                err = None if isinstance(args, dict) else "arguments are not a JSON object"
                if err:
                    args = None
            except json.JSONDecodeError as exc:
                args, err = None, f"invalid JSON arguments: {exc}"
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name") or "", arguments=args,
                                  raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
                                  parse_error=err))
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ModelResponse(
            route_id=route.id, model=route.model, text=strip_thinking(content), tool_calls=calls,
            finish_reason=choice.get("finish_reason"), usage=raw.get("usage"), call_id=call_id, latency_ms=latency,
            image_transforms=transforms, reasoning=msg.get("reasoning_content") or msg.get("reasoning"), raw=None,
        )
