"""OpenAI-compatible metering gateway for framework-owned model calls.

OpenClaw is configured with a single custom provider pointing here
(``models.mode: "replace"``), so its planning and helper calls pass the same allowlist,
modality checks and shared budget as application calls. The requested ``model`` names
a role (``planning``, ``vision``, ...) or an allowlisted route id; anything else is refused.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from ..budget import (
    BudgetBlocked,
    CallEstimate,
    Usage,
    estimate_image_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
)
from ..config import ModelRoute
from .providers import ProviderError
from .registry import RouteRejected
from .router import ModelCallError, ModelRouter
from .types import count_images

ROLES = ("planning", "vision", "summarisation", "review")


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code

    def body(self) -> dict[str, Any]:
        return {"error": {"message": str(self), "type": self.code, "code": self.code}}


def list_models(router: ModelRouter) -> dict[str, Any]:
    data = []
    for role in ROLES:
        try:
            chain = router.registry.chain(role, {"text"})
        except RouteRejected:
            continue
        r = chain[0]
        data.append({"id": role, "object": "model", "owned_by": "omca", "route": r.id,
                     "input": [m for m in r.modalities if m in ("text", "image")], "context_window": r.context_limit})
    return {"object": "list", "data": data}


def _resolve(router: ModelRouter, requested: str, needs: set[str]) -> list[ModelRoute]:
    name = requested.split("/", 1)[-1]
    try:
        if name in ROLES:
            return router.registry.chain(name, needs - {"tools"})
        return [router.registry.require_usable(name, needs - {"tools"})]
    except RouteRejected as exc:
        raise GatewayError(403, "route_rejected", str(exc)) from exc


async def chat_completions(router: ModelRouter, body: dict[str, Any], caller: str = "openclaw") -> dict[str, Any]:
    if body.get("stream"):
        raise GatewayError(400, "use_stream", "call stream_chat_completions for streaming requests")
    messages = body.get("messages") or []
    needs = {"text"} | ({"image"} if count_images(messages) else set()) | ({"tools"} if body.get("tools") else set())
    chain = _resolve(router, str(body.get("model", "")), needs)
    last: Exception | None = None
    for route in chain:
        try:
            resp = await router.call_route(
                route.id, messages, tools=body.get("tools"), tool_choice=body.get("tool_choice"),
                max_output_tokens=body.get("max_tokens") or body.get("max_completion_tokens"),
                temperature=body.get("temperature"), purpose="framework", caller=caller,
                response_format=body.get("response_format"),
            )
        except BudgetBlocked as exc:
            raise GatewayError(402, "budget_blocked", str(exc)) from exc
        except ModelCallError as exc:
            raise GatewayError(403 if exc.code.value == "ROUTE_REJECTED" else 400, exc.code.value.lower(), str(exc)) from exc
        except ProviderError as exc:
            last = exc
            continue
        msg: dict[str, Any] = {"role": "assistant", "content": resp.text}
        if resp.tool_calls:
            msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.name, "arguments": tc.raw_arguments}} for tc in resp.tool_calls]
        return {
            "id": f"chatcmpl-{resp.call_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model"),
            "choices": [{"index": 0, "message": msg, "finish_reason": resp.finish_reason or "stop"}],
            "usage": resp.usage or {},
        }
    raise GatewayError(502, "upstream_error", f"all routes failed: {last}")


async def stream_chat_completions(router: ModelRouter, body: dict[str, Any], caller: str = "openclaw") -> AsyncIterator[str]:
    """Pass an SSE stream through while metering it. Missing final usage keeps the reservation as UNKNOWN."""
    messages = body.get("messages") or []
    needs = {"text"} | ({"image"} if count_images(messages) else set()) | ({"tools"} if body.get("tools") else set())
    route = _resolve(router, str(body.get("model", "")), needs)[0]
    max_tokens = min(body.get("max_tokens") or body.get("max_completion_tokens") or route.max_output_tokens,
                     route.max_output_tokens)
    n_images = len(count_images(messages))
    est = CallEstimate(
        input_tokens=estimate_message_tokens(messages) + (estimate_text_tokens(json.dumps(body.get("tools"))) if body.get("tools") else 0),
        images=n_images,
        image_tokens=n_images * estimate_image_tokens(route.max_image_side, route.max_image_side, route.image_patch_px,
                                                      route.max_image_side),
        max_output_tokens=max_tokens,
    )
    try:
        call_id = router.ledger.reserve(route, est, task_id=None, purpose="framework", caller=caller)
    except BudgetBlocked as exc:
        raise GatewayError(402, "budget_blocked", str(exc)) from exc
    upstream = {k: v for k, v in body.items() if k not in {"model", "max_completion_tokens"}}
    upstream.update({"model": route.model, "max_tokens": max_tokens, "stream": True,
                     "stream_options": {"include_usage": True}})
    upstream.update(route.extra_body)
    started = time.monotonic()
    usage: dict[str, Any] | None = None
    finished = False
    try:
        async for line in router.provider(route).stream(route, upstream):
            if not line:
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    yield "data: [DONE]\n\n"
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                chunk["model"] = body.get("model")
                yield "data: " + json.dumps(chunk) + "\n\n"
    except ProviderError as exc:
        (router.ledger.mark_unknown if exc.billed_possible else router.ledger.release)(call_id, str(exc))
        yield "data: " + json.dumps({"error": {"message": str(exc), "type": "upstream_error"}}) + "\n\n"
        return
    except BaseException:
        router.ledger.mark_unknown(call_id, "stream interrupted")
        raise
    latency = int((time.monotonic() - started) * 1000)
    u = Usage(usage.get("prompt_tokens"), usage.get("completion_tokens")) if usage and route.usage_reporting else None
    router.ledger.settle(call_id, route, u, images=n_images, latency_ms=latency)
    if not finished:
        yield "data: [DONE]\n\n"
