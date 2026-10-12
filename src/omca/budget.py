"""Shared paid-inference budget (spec section 18).

* Before a hosted call, a conservative cost bound is reserved atomically against both the
  monthly ceiling and the per-task ceiling (one IMMEDIATE transaction, single writer).
* Actual usage is settled afterwards. Calls whose usage is unknown keep their reservation
  until an operator reconciles them.
* Prices and FX rates are configuration with a recorded source and date. A token-billed
  route without prices, or without an FX rate into the budget currency, stays disabled.
* Time-billed dedicated endpoints reserve their approved running window (including
  initialization) up front and settle on stop.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ApplicationConfig, ModelRoute
from .contracts import BudgetStatus, ErrorCode
from .db import Database
from .util import iso, new_id, now_iso, parse_iso, utcnow


class BudgetBlocked(Exception):
    code = ErrorCode.BUDGET_BLOCKED

    def __init__(self, message: str, *, scope: str = "monthly"):
        super().__init__(message)
        self.scope = scope


@dataclass
class CallEstimate:
    input_tokens: int
    images: int = 0
    image_tokens: int = 0
    max_output_tokens: int = 1024


@dataclass
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    image_tokens: int | None = None
    source: str = "provider"


class BudgetLedger:
    def __init__(self, db: Database, config: ApplicationConfig):
        self.db = db
        self.config = config
        self.bc = config.budget
        # called with (call_id, route_id, reserved, actual) when a call cost more than it reserved
        self.on_overrun: Callable[[str, str, float, float], None] | None = None

    # -- periods and money ------------------------------------------------------
    def period(self, at: datetime | None = None) -> str:
        at = at or utcnow()
        return at.astimezone(ZoneInfo(self.bc.period_timezone)).strftime("%Y-%m")

    def fx(self, currency: str) -> float | None:
        if currency == self.bc.currency:
            return 1.0
        rate = self.bc.fx.get(currency)
        return rate.rate if rate and rate.rate else None

    def route_billing_problem(self, route: ModelRoute) -> str | None:
        """Why a route cannot be metered; ``None`` when it can."""
        if route.billing == "none":
            return None
        p = route.pricing
        if route.billing == "tokens" and (p.input_per_million is None or p.output_per_million is None):
            return "hosted prices are not configured"
        if route.billing == "time" and p.per_hour is None:
            return "hourly price is not configured"
        if not p.source or not p.effective_date:
            return "price source and effective date are not recorded"
        if self.fx(p.currency) is None:
            return f"FX rate {p.currency}->{self.bc.currency} is not configured"
        return None

    def estimate(self, route: ModelRoute, est: CallEstimate) -> float:
        if route.billing != "tokens":
            return 0.0
        p = route.pricing
        rate = self.fx(p.currency)
        if rate is None or p.input_per_million is None or p.output_per_million is None:
            raise BudgetBlocked(f"route {route.id} cannot be metered: {self.route_billing_problem(route)}")
        cost = (est.input_tokens + est.image_tokens) * p.input_per_million / 1e6
        cost += est.max_output_tokens * p.output_per_million / 1e6
        cost += est.images * (p.image_per_unit or 0.0)
        return round(cost * rate * (1.0 + self.bc.reserve_margin), 6)

    def actual(self, route: ModelRoute, usage: Usage, images: int) -> float:
        if route.billing != "tokens":
            return 0.0
        p = route.pricing
        rate = self.fx(p.currency) or 0.0
        cost = (usage.input_tokens or 0) * (p.input_per_million or 0) / 1e6
        cost += (usage.output_tokens or 0) * (p.output_per_million or 0) / 1e6
        cost += images * (p.image_per_unit or 0.0)
        return round(cost * rate, 6)

    # -- spend aggregates ----------------------------------------------------------
    def _period_spend(self, c, period: str) -> tuple[float, float, float]:
        row = c.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status='SETTLED' THEN cost ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN status='RESERVED' THEN reservation ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN status='UNKNOWN' THEN MAX(reservation, COALESCE(cost, 0)) ELSE 0 END), 0)
               FROM model_calls WHERE period=?""",
            (period,),
        ).fetchone()
        settled, reserved, unknown = float(row[0]), float(row[1]), float(row[2])
        erow = c.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status='SETTLED' THEN cost ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN status IN ('RESERVED','RUNNING') THEN MAX(reservation, COALESCE(cost, 0))
                              ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN status='UNKNOWN' THEN reservation ELSE 0 END), 0)
               FROM endpoint_sessions WHERE period=?""",
            (period,),
        ).fetchone()
        return settled + float(erow[0]), reserved + float(erow[1]), unknown + float(erow[2])

    def _task_spend(self, c, task_id: str) -> float:
        row = c.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='SETTLED' THEN cost
                                        WHEN status IN ('RESERVED','UNKNOWN') THEN MAX(reservation, COALESCE(cost,0))
                                        ELSE 0 END), 0)
               FROM model_calls WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        return float(row[0])

    def task_spend(self, task_id: str) -> float:
        with self.db.tx() as c:
            return self._task_spend(c, task_id)

    # -- reservation lifecycle --------------------------------------------------------
    def reserve(
        self,
        route: ModelRoute,
        est: CallEstimate,
        *,
        task_id: str | None,
        purpose: str,
        caller: str,
        task_ceiling: float | None = None,
        attempt: int = 1,
    ) -> str:
        call_id = new_id("mdl")
        now = now_iso()
        period = self.period()
        if route.billing == "time":
            amount = 0.0  # covered by the endpoint session reservation
            if not self._has_running_session(route.id):
                raise BudgetBlocked(f"dedicated endpoint {route.id} has no approved running session", scope="endpoint")
        else:
            problem = self.route_billing_problem(route)
            if problem:
                raise BudgetBlocked(f"route {route.id} is disabled: {problem}", scope="route")
            amount = self.estimate(route, est)
        refusal: BudgetBlocked | None = None
        with self.db.tx() as c:
            if amount > 0:
                settled, reserved, unknown = self._period_spend(c, period)
                if settled + reserved + unknown + amount > self.bc.monthly_ceiling + 1e-9:
                    self._refused(c, call_id, route, est, task_id, purpose, caller, period, amount, "monthly ceiling")
                    refusal = BudgetBlocked(
                        f"monthly budget would be exceeded: {settled + reserved + unknown:.2f} committed + "
                        f"{amount:.2f} requested > {self.bc.monthly_ceiling:.2f} {self.bc.currency}"
                    )
                elif task_id is not None:
                    ceiling = task_ceiling if task_ceiling is not None else self.bc.per_task_reservation_ceiling
                    used = self._task_spend(c, task_id)
                    if used + amount > ceiling + 1e-9:
                        self._refused(c, call_id, route, est, task_id, purpose, caller, period, amount, "task ceiling")
                        refusal = BudgetBlocked(
                            f"task budget would be exceeded: {used:.2f} + {amount:.2f} > {ceiling:.2f} {self.bc.currency}",
                            scope="task",
                        )
            if refusal is None:
                c.execute(
                    """INSERT INTO model_calls(id, task_id, route_id, purpose, caller, period, status, est_input_tokens,
                       est_images, max_output_tokens, reservation, currency, attempt, created_at)
                       VALUES (?,?,?,?,?,?,'RESERVED',?,?,?,?,?,?,?)""",
                    (call_id, task_id, route.id, purpose, caller, period, est.input_tokens + est.image_tokens,
                     est.images, est.max_output_tokens, amount, self.bc.currency, attempt, now),
                )
        if refusal is not None:
            raise refusal
        return call_id

    def _refused(self, c, call_id, route, est, task_id, purpose, caller, period, amount, why) -> None:
        c.execute(
            """INSERT INTO model_calls(id, task_id, route_id, purpose, caller, period, status, est_input_tokens,
               est_images, max_output_tokens, reservation, currency, error, created_at)
               VALUES (?,?,?,?,?,?,'REFUSED',?,?,?,0,?,?,?)""",
            (call_id, task_id, route.id, purpose, caller, period, est.input_tokens + est.image_tokens, est.images,
             est.max_output_tokens, self.bc.currency, f"refused: {why} (requested {amount:.4f})", now_iso()),
        )

    def settle(self, call_id: str, route: ModelRoute, usage: Usage | None, *, images: int, latency_ms: int) -> float | None:
        """Record actual usage. Missing usage keeps the conservative reservation as UNKNOWN."""
        if usage is None or (route.billing == "tokens" and (usage.input_tokens is None or usage.output_tokens is None)):
            self.db.execute(
                "UPDATE model_calls SET status='UNKNOWN', latency_ms=?, usage_source='missing', settled_at=? WHERE id=?",
                (latency_ms, now_iso(), call_id),
            )
            return None
        cost = self.actual(route, usage, images)
        reserved = self.db.scalar("SELECT reservation FROM model_calls WHERE id=?", (call_id,)) or 0.0
        overrun = cost > reserved + 1e-9 and route.billing == "tokens"
        self.db.execute(
            """UPDATE model_calls SET status='SETTLED', input_tokens=?, output_tokens=?, image_tokens=?, cost=?,
               latency_ms=?, usage_source=?, settled_at=?, error=? WHERE id=?""",
            (usage.input_tokens, usage.output_tokens, usage.image_tokens, cost, latency_ms, usage.source, now_iso(),
             f"actual cost {cost:.4f} exceeded the reservation {reserved:.4f}" if overrun else None, call_id),
        )
        if overrun and self.on_overrun is not None:
            self.on_overrun(call_id, route.id, reserved, cost)
        return cost

    def release(self, call_id: str, error: str) -> None:
        """The request never reached the provider: nothing was billed."""
        self.db.execute("UPDATE model_calls SET status='RELEASED', cost=0, error=?, settled_at=? WHERE id=?",
                        (error[:500], now_iso(), call_id))

    def mark_unknown(self, call_id: str, error: str) -> None:
        """The request may have been billed (timeout, dropped stream): keep the reservation."""
        self.db.execute("UPDATE model_calls SET status='UNKNOWN', error=?, settled_at=? WHERE id=?",
                        (error[:500], now_iso(), call_id))

    def reconcile(self, call_id: str, actual_cost: float, note: str = "operator reconciliation") -> None:
        self.db.execute(
            "UPDATE model_calls SET status='SETTLED', cost=?, usage_source=?, settled_at=? WHERE id=? AND status IN ('UNKNOWN','RESERVED')",
            (actual_cost, note, now_iso(), call_id),
        )

    def recover_after_restart(self) -> int:
        """Calls that were in flight when the process stopped may have been billed."""
        rows = self.db.query("SELECT id FROM model_calls WHERE status='RESERVED'")
        for r in rows:
            self.mark_unknown(r["id"], "process restarted while the call was in flight")
        return len(rows)

    # -- dedicated (time-billed) endpoints ---------------------------------------------
    def _has_running_session(self, route_id: str) -> bool:
        now = now_iso()
        return bool(self.db.scalar(
            "SELECT 1 FROM endpoint_sessions WHERE route_id=? AND status='RUNNING' AND approved_until > ?", (route_id, now)
        ))

    def reserve_endpoint_session(self, route: ModelRoute, hours: float) -> str:
        if route.billing != "time" or route.dedicated is None:
            raise ValueError(f"route {route.id} is not a time-billed dedicated endpoint")
        problem = self.route_billing_problem(route)
        if problem:
            raise BudgetBlocked(f"route {route.id} is disabled: {problem}", scope="route")
        hours = min(hours, route.dedicated.max_session_hours)
        billable_hours = hours + route.dedicated.initialization_minutes / 60.0
        amount = billable_hours * route.pricing.per_hour * self.fx(route.pricing.currency) * (1 + self.bc.reserve_margin)  # type: ignore[operator]
        session_id = new_id("eps")
        period = self.period()
        start = utcnow()
        with self.db.tx() as c:
            settled, reserved, unknown = self._period_spend(c, period)
            if settled + reserved + unknown + amount > self.bc.monthly_ceiling + 1e-9:
                raise BudgetBlocked(f"endpoint session needs {amount:.2f} {self.bc.currency}; monthly budget exhausted")
            c.execute(
                """INSERT INTO endpoint_sessions(id, route_id, period, status, reservation, currency, started_at,
                   approved_until) VALUES (?,?,?,'RUNNING',?,?,?,?)""",
                (session_id, route.id, period, round(amount, 6), self.bc.currency, iso(start),
                 iso(start + timedelta(hours=hours))),
            )
        return session_id

    def settle_endpoint_session(self, session_id: str, route: ModelRoute, stopped_at: datetime | None = None) -> float:
        r = self.db.one("SELECT * FROM endpoint_sessions WHERE id=?", (session_id,))
        if r is None:
            raise KeyError(session_id)
        stopped = stopped_at or utcnow()
        started = parse_iso(r["started_at"])
        hours = max(0.0, (stopped - started).total_seconds() / 3600.0)  # type: ignore[operator]
        init = route.dedicated.initialization_minutes / 60.0 if route.dedicated else 0.0
        cost = round((hours + init) * (route.pricing.per_hour or 0) * (self.fx(route.pricing.currency) or 0), 6)
        self.db.execute("UPDATE endpoint_sessions SET status='SETTLED', cost=?, stopped_at=? WHERE id=?",
                        (cost, iso(stopped), session_id))
        return cost

    def accrue_endpoint_session(self, session_id: str, route: ModelRoute) -> float:
        """Record the running cost so far of a session that could not be stopped (it keeps counting)."""
        r = self.db.one("SELECT * FROM endpoint_sessions WHERE id=?", (session_id,))
        if r is None:
            raise KeyError(session_id)
        hours = max(0.0, (utcnow() - parse_iso(r["started_at"])).total_seconds() / 3600.0)  # type: ignore[operator]
        init = route.dedicated.initialization_minutes / 60.0 if route.dedicated else 0.0
        cost = round((hours + init) * (route.pricing.per_hour or 0) * (self.fx(route.pricing.currency) or 0), 6)
        self.db.execute("UPDATE endpoint_sessions SET cost=? WHERE id=? AND status='RUNNING'", (cost, session_id))
        return cost

    def end_endpoint_window(self, session_id: str) -> None:
        """Close the approved window now, so the maintenance loop keeps trying to pause the endpoint."""
        self.db.execute("UPDATE endpoint_sessions SET approved_until=? WHERE id=? AND status='RUNNING'",
                        (now_iso(), session_id))

    def overdue_endpoint_sessions(self) -> list[tuple[str, str]]:
        rows = self.db.query("SELECT id, route_id FROM endpoint_sessions WHERE status='RUNNING' AND approved_until <= ?",
                             (now_iso(),))
        return [(r["id"], r["route_id"]) for r in rows]

    # -- status and notices --------------------------------------------------------------
    def status(self) -> BudgetStatus:
        period = self.period()
        with self.db.tx() as c:
            settled, reserved, unknown = self._period_spend(c, period)
        committed = settled + reserved + unknown
        util = committed / self.bc.monthly_ceiling if self.bc.monthly_ceiling else 1.0
        enabled, disabled = [], {}
        for r in self.config.models.routes:
            if r.billing == "none":
                continue
            problem = self.route_billing_problem(r) if r.enabled else "route is disabled in configuration"
            if problem:
                disabled[r.id] = problem
            else:
                enabled.append(r.id)
        return BudgetStatus(
            currency=self.bc.currency,
            period=period,
            period_timezone=self.bc.period_timezone,
            ceiling=self.bc.monthly_ceiling,
            settled=round(settled, 4),
            reserved=round(reserved, 4),
            unknown=round(unknown, 4),
            remaining=round(max(0.0, self.bc.monthly_ceiling - committed), 4),
            utilisation=round(util, 4),
            thresholds_crossed=[t for t in self.bc.warning_thresholds if util >= t],
            per_task_ceiling=self.bc.per_task_reservation_ceiling,
            hosted_routes_enabled=enabled,
            disabled_routes=disabled,
        )

    def new_threshold_notices(self) -> list[float]:
        st = self.status()
        out = []
        for t in st.thresholds_crossed:
            with self.db.tx() as c:
                cur = c.execute("INSERT OR IGNORE INTO budget_notices(period, threshold, sent_at) VALUES (?,?,?)",
                                (st.period, t, now_iso()))
                if cur.rowcount:
                    out.append(t)
        return out

    def ledger_rows(self, period: str | None = None, limit: int = 200) -> list[dict]:
        period = period or self.period()
        rows = self.db.query("SELECT * FROM model_calls WHERE period=? ORDER BY created_at DESC LIMIT ?", (period, limit))
        return [dict(r) for r in rows]


MESSAGE_OVERHEAD_TOKENS = 8  # role markers and template tokens per chat message


def estimate_text_tokens(text: str) -> int:
    """Conservative token estimate.

    ASCII text averages about 4 characters per token, counted here as 3. Other scripts (Devanagari,
    CJK, ...) take 2-3 UTF-8 bytes per character and tokenise far less densely, so they are counted by
    UTF-8 bytes: one token per 2 bytes.
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_bytes = len(text.encode("utf-8", "replace")) - ascii_chars
    return math.ceil(ascii_chars / 3.0 + other_bytes / 2.0) + 4


def estimate_message_tokens(messages: list[dict]) -> int:
    from .models.types import text_of

    return sum(estimate_text_tokens(text_of([m])) + MESSAGE_OVERHEAD_TOKENS for m in messages)


def estimate_image_tokens(width: int, height: int, patch: int = 28, max_side: int = 1280) -> int:
    scale = min(1.0, max_side / max(width, height, 1))
    w, h = width * scale, height * scale
    return int(math.ceil(w / patch) * math.ceil(h / patch) * 1.1) + 64
