"""Open-model route registry and allowlist (spec section 13).

A route can serve a call only when *all* of these hold:

* it is declared in configuration (the allowlist) and enabled;
* it records open weights with a license and the model name is not a known closed model;
* billing can be metered (local routes are free; hosted routes need prices and FX);
* the required capability probes passed for the route's current configuration digest;
* it supports the modalities the call needs (e.g. images for vision).

Every application-controlled role, including fallbacks, resolves through this registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..budget import BudgetLedger
from ..config import ApplicationConfig, ModelRoute
from ..contracts import ErrorCode
from ..db import Database, dumps, loads
from ..util import digest, now_iso

CLOSED_MODEL_PATTERNS = [
    r"(^|/)gpt-", r"(^|/)o[1345](-|$)", r"(^|/)chatgpt", r"(^|/)claude", r"(^|/)gemini-", r"(^|/)grok",
    r"(^|/)command-r", r"(^|/)mistral-(large|medium)", r"(^|/)sonar", r"(^|/)reka-", r"(^|/)ernie", r"(^|/)kimi-k2-thinking-turbo",
]
CLOSED_LICENSES = {"proprietary", "closed", "commercial", "none", "unknown", ""}

TEXT_PROBES = ["text", "structured_tool_call", "tool_result_continuation", "bounded_output", "usage_reporting",
               "disabled_route_rejection"]
IMAGE_PROBES = ["screenshot_interpretation", "image_with_tools"]


class RouteRejected(Exception):
    code = ErrorCode.ROUTE_REJECTED


@dataclass
class RouteState:
    route: ModelRoute
    usable: bool
    reasons: list[str]


def route_digest(route: ModelRoute) -> str:
    return digest({
        "provider": route.provider, "base_url": route.base_url, "model": route.model, "revision": route.revision,
        "modalities": sorted(route.modalities), "tools": route.supports_tools, "extra_body": route.extra_body,
    })


def required_probes(route: ModelRoute) -> list[str]:
    return TEXT_PROBES + (IMAGE_PROBES if route.supports("image") else [])


class ModelRegistry:
    def __init__(self, db: Database, config: ApplicationConfig, ledger: BudgetLedger):
        self.db = db
        self.config = config
        self.ledger = ledger

    # -- allowlist ----------------------------------------------------------------
    def open_model_problem(self, route: ModelRoute) -> str | None:
        if not route.open_weights:
            return "route does not record open weights"
        if (route.license or "").strip().lower() in CLOSED_LICENSES:
            return f"license {route.license!r} does not permit the intended use"
        name = route.model.lower()
        for pat in CLOSED_MODEL_PATTERNS:
            if re.search(pat, name):
                return f"model {route.model} matches the closed-model denylist"
        if route.provider == "fixture" and not self.config.models.allow_fixture_routes:
            return "fixture routes are test-only"
        return None

    def probes_passed(self, route: ModelRoute) -> tuple[bool, list[str]]:
        d = route_digest(route)
        rows = self.db.query("SELECT probe, passed FROM route_probes WHERE route_id=? AND route_digest=?", (route.id, d))
        passed = {r["probe"] for r in rows if r["passed"]}
        missing = [p for p in required_probes(route) if p not in passed]
        return not missing, missing

    def state(self, route: ModelRoute) -> RouteState:
        reasons = []
        problem = self.open_model_problem(route)
        if problem:
            reasons.append(problem)
        if not route.enabled:
            reasons.append("route is disabled in configuration")
        billing = self.ledger.route_billing_problem(route)
        if billing:
            reasons.append(billing)
        if self.config.models.require_probes:
            ok, missing = self.probes_passed(route)
            if not ok:
                reasons.append(f"capability probes not passed: {', '.join(missing)}")
        return RouteState(route, not reasons, reasons)

    def get(self, route_id: str) -> ModelRoute:
        route = self.config.models.route(route_id)
        if route is None:
            raise RouteRejected(f"model route {route_id!r} is not on the allowlist")
        return route

    def require_usable(self, route_id: str, needs: set[str] | None = None) -> ModelRoute:
        route = self.get(route_id)
        st = self.state(route)
        if not st.usable:
            raise RouteRejected(f"route {route_id} rejected: {'; '.join(st.reasons)}")
        for m in needs or set():
            if not route.supports(m):
                raise RouteRejected(f"route {route_id} does not support {m} input")
        if "tools" in (needs or set()) and not route.supports_tools:
            raise RouteRejected(f"route {route_id} does not support tool calls")
        return route

    def chain(self, role: str, needs: set[str]) -> list[ModelRoute]:
        """Primary route for a role followed by allowlisted fallbacks that fit the modalities."""
        roles = self.config.models.roles
        primary = getattr(roles, role, None)
        if primary is None and role == "summarisation":
            primary = roles.planning
        if primary is None and role == "review":
            primary = roles.summarisation or roles.planning
        if primary is None:
            raise RouteRejected(f"no model route is configured for role {role}")
        ids = [primary, *roles.fallbacks.get(role, [])]
        usable, errors = [], []
        for rid in ids:
            try:
                usable.append(self.require_usable(rid, {m for m in needs if m != "tools"}))
            except RouteRejected as exc:
                errors.append(str(exc))
        if not usable:
            raise RouteRejected(f"no usable route for role {role}: " + " | ".join(errors))
        return usable

    # -- probe records --------------------------------------------------------------
    def record_probe(self, route: ModelRoute, probe: str, passed: bool, detail: dict) -> None:
        self.db.execute(
            """INSERT INTO route_probes(route_id, route_digest, probe, passed, detail_json, ran_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(route_id, route_digest, probe) DO UPDATE SET passed=excluded.passed,
               detail_json=excluded.detail_json, ran_at=excluded.ran_at""",
            (route.id, route_digest(route), probe, int(passed), dumps(detail), now_iso()),
        )

    def probe_report(self, route: ModelRoute) -> list[dict]:
        rows = self.db.query("SELECT * FROM route_probes WHERE route_id=? AND route_digest=? ORDER BY probe",
                             (route.id, route_digest(route)))
        return [{"probe": r["probe"], "passed": bool(r["passed"]), "ran_at": r["ran_at"],
                 "detail": loads(r["detail_json"], {})} for r in rows]
