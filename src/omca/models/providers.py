"""Provider adapters.

All supported routes speak the OpenAI chat-completions protocol: DeepInfra, Ollama's
``/v1`` endpoint, Hugging Face Inference Providers (router) and dedicated endpoints running
TGI or vLLM. Route-specific quirks live in route configuration (``extra_body``), not code.

``FixtureProvider`` is a deterministic, test-only provider whose behaviour is registered
by name; configuration refuses fixture routes unless ``models.allow_fixture_routes`` is set.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from ..config import ModelRoute
from ..secrets import resolve


class ProviderError(Exception):
    """``billed_possible`` decides whether the reservation is released or kept as UNKNOWN."""

    def __init__(self, message: str, *, retryable: bool, billed_possible: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.billed_possible = billed_possible
        self.status = status


class Provider:
    async def chat(self, route: ModelRoute, body: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stream(self, route: ModelRoute, body: dict[str, Any]):  # pragma: no cover - interface
        raise NotImplementedError
        yield b""


class OpenAICompatibleProvider(Provider):
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    def _headers(self, route: ModelRoute) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = resolve(route.auth_ref)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _url(self, route: ModelRoute) -> str:
        base = (route.base_url or "").rstrip("/")
        return f"{base}/chat/completions"

    async def chat(self, route: ModelRoute, body: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=route.request_timeout_seconds)
        try:
            resp = await client.post(self._url(route), json=body, headers=self._headers(route),
                                     timeout=route.request_timeout_seconds)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError(f"cannot reach {route.id}: {exc}", retryable=True, billed_possible=False) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{route.id} timed out after sending the request", retryable=False,
                                billed_possible=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport error from {route.id}: {exc}", retryable=False, billed_possible=True) from exc
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code >= 400:
            text = resp.text[:500]
            retry = resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504)
            raise ProviderError(f"{route.id} returned HTTP {resp.status_code}: {text}", retryable=retry,
                                billed_possible=False, status=resp.status_code)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{route.id} returned invalid JSON", retryable=False, billed_possible=True) from exc

    async def stream(self, route: ModelRoute, body: dict[str, Any]):
        client = self._client or httpx.AsyncClient(timeout=route.request_timeout_seconds)
        try:
            async with client.stream("POST", self._url(route), json=body, headers=self._headers(route),
                                     timeout=route.request_timeout_seconds) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread())[:500].decode("utf-8", "replace")
                    raise ProviderError(f"{route.id} returned HTTP {resp.status_code}: {text}",
                                        retryable=resp.status_code in (429, 500, 502, 503, 504),
                                        billed_possible=False, status=resp.status_code)
                async for line in resp.aiter_lines():
                    yield line
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError(f"cannot reach {route.id}: {exc}", retryable=True, billed_possible=False) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{route.id} stream timed out", retryable=False, billed_possible=True) from exc
        finally:
            if self._client is None:
                await client.aclose()


FixtureHandler = Callable[[ModelRoute, dict[str, Any]], dict[str, Any]]
FIXTURE_HANDLERS: dict[str, FixtureHandler] = {}


def register_fixture(name: str, handler: FixtureHandler) -> None:
    FIXTURE_HANDLERS[name] = handler


def fixture_reply(text: str = "", tool_calls: list[dict[str, Any]] | None = None, *, prompt_tokens: int = 100,
                  completion_tokens: int = 20, finish_reason: str | None = None, usage: bool = True) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": tc.get("id", f"call_{i}"), "type": "function",
             "function": {"name": tc["name"],
                          "arguments": tc["arguments"] if isinstance(tc["arguments"], str) else json.dumps(tc["arguments"])}}
            for i, tc in enumerate(tool_calls)
        ]
    out: dict[str, Any] = {
        "id": "fixture",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop")}],
    }
    if usage:
        out["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens}
    return out


class FixtureProvider(Provider):
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def chat(self, route: ModelRoute, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((route.id, body))
        handler = FIXTURE_HANDLERS.get(route.fixture or "")
        if handler is None:
            raise ProviderError(f"no fixture handler named {route.fixture!r}", retryable=False, billed_possible=False)
        result = handler(route, body)
        if isinstance(result, Exception):
            raise result
        return result

    async def stream(self, route: ModelRoute, body: dict[str, Any]):
        result = await self.chat(route, {**body, "stream": False})
        msg = result["choices"][0]["message"]
        chunk = {"id": "fixture", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg.get("content") or ""},
                              "finish_reason": None}]}
        yield "data: " + json.dumps(chunk)
        final = {"id": "fixture", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": result["choices"][0]["finish_reason"]}],
                 "usage": result.get("usage")}
        yield "data: " + json.dumps(final)
        yield "data: [DONE]"
