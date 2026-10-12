"""Pause control for time-billed dedicated endpoints (spec section 18, reference S19).

A dedicated endpoint runs only inside an owner-approved window whose cost (including
initialization) is reserved up front against the shared budget. When the window ends the
coordinator pauses the endpoint and settles the actual running time, so idle time never
escapes accounting.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..budget import BudgetLedger
from ..config import ModelRoute
from ..secrets import require


class EndpointControlError(Exception):
    pass


class EndpointController:
    async def resume(self, route: ModelRoute) -> dict[str, Any]:
        raise NotImplementedError

    async def pause(self, route: ModelRoute) -> dict[str, Any]:
        raise NotImplementedError


class HuggingFaceEndpointController(EndpointController):
    """Hugging Face Inference Endpoints management API (``/v2/endpoint/{namespace}/{name}/pause|resume``)."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    async def _post(self, route: ModelRoute, action: str) -> dict[str, Any]:
        d = route.dedicated
        if d is None or not d.namespace or not d.name:
            raise EndpointControlError(f"route {route.id} has no dedicated endpoint namespace/name")
        token = require(d.api_token_ref, f"endpoint control token for {route.id}")
        url = f"{d.api_base.rstrip('/')}/endpoint/{d.namespace}/{d.name}/{action}"
        client = self.client or httpx.AsyncClient(timeout=60)
        try:
            resp = await client.post(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise EndpointControlError(f"{action} failed: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code >= 400:
            raise EndpointControlError(f"{action} returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    async def resume(self, route: ModelRoute) -> dict[str, Any]:
        return await self._post(route, "resume")

    async def pause(self, route: ModelRoute) -> dict[str, Any]:
        return await self._post(route, "pause")


class ManualEndpointController(EndpointController):
    """The operator starts and pauses the endpoint; the ledger still reserves and settles the window."""

    async def resume(self, route: ModelRoute) -> dict[str, Any]:
        return {"manual": True}

    async def pause(self, route: ModelRoute) -> dict[str, Any]:
        return {"manual": True}


def controller_for(route: ModelRoute, client: httpx.AsyncClient | None = None) -> EndpointController:
    if route.dedicated is None:
        raise EndpointControlError(f"route {route.id} is not a dedicated endpoint")
    if route.dedicated.controller == "huggingface":
        return HuggingFaceEndpointController(client)
    return ManualEndpointController()


async def start_window(ledger: BudgetLedger, route: ModelRoute, hours: float,
                       controller: EndpointController | None = None) -> str:
    """Reserve the window first; only then resume the endpoint. A failed resume settles at once."""
    session_id = ledger.reserve_endpoint_session(route, hours)
    try:
        await (controller or controller_for(route)).resume(route)
    except Exception:
        ledger.settle_endpoint_session(session_id, route)
        raise
    return session_id


async def stop_window(ledger: BudgetLedger, route: ModelRoute, session_id: str,
                      controller: EndpointController | None = None) -> float:
    await (controller or controller_for(route)).pause(route)
    return ledger.settle_endpoint_session(session_id, route)
