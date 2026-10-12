"""Durable schedules and standing goals (spec section 17; FR20, FR21).

A firing creates a task whose deduplication key is derived from the schedule id and the
planned firing time, so restarts and duplicate triggers (internal loop or an OpenClaw
automation calling ``fire``) never create a second run. Overlapping runs are skipped unless
the schedule allows them. Machine timestamps are UTC; schedules evaluate in their timezone.
Recurring work never grants new permissions: its tasks pass the same policy gate.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .config import ScheduleConfig
from .contracts import TERMINAL_STATUSES, TaskBudget, TaskRequest
from .db import dumps, loads
from .util import UTC, iso, new_id, now_iso, parse_iso, utcnow

if TYPE_CHECKING:
    from .coordinator import Coordinator

NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
         "nov": 11, "dec": 12, "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


class CronError(ValueError):
    pass


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step <= 0:
                raise CronError("cron step must be positive")
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = int(NAMES.get(x, x)), int(NAMES.get(y, y))
        else:
            a = int(NAMES.get(part, part))
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise CronError(f"cron value {part} out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("cron expressions need 5 fields: minute hour day-of-month month day-of-week")
        self.expr = expr
        self.minutes = _field(parts[0], 0, 59)
        self.hours = _field(parts[1], 0, 23)
        self.doms = _field(parts[2], 1, 31)
        self.months = _field(parts[3], 1, 12)
        dows = _field(parts[4].replace("7", "0") if re.fullmatch(r"[0-7,\-*/]+", parts[4]) else parts[4], 0, 7)
        self.dows = {d % 7 for d in dows}
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def _day_ok(self, d: datetime) -> bool:
        dom_ok = d.day in self.doms
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime, tz: str) -> datetime:
        """Next firing strictly after ``after`` (aware), returned in UTC."""
        zone = ZoneInfo(tz)
        local = after.astimezone(zone).replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        for _ in range(600000):
            if local.month not in self.months:
                local = (local.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(local):
                local = local.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if local.hour not in self.hours:
                local = local.replace(minute=0) + timedelta(hours=1)
                continue
            if local.minute not in self.minutes:
                local += timedelta(minutes=1)
                continue
            aware = local.replace(tzinfo=zone)
            # skip wall-clock times that do not exist (DST gap)
            if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
                local += timedelta(minutes=1)
                continue
            result = aware.astimezone(UTC)
            if result > after:
                return result
            local += timedelta(minutes=1)
        raise CronError(f"no firing time found for {self.expr}")


def render_goal(template: str, planned: datetime, tz: str, name: str) -> str:
    local = planned.astimezone(ZoneInfo(tz))
    return (template.replace("{date}", local.strftime("%Y-%m-%d")).replace("{datetime}", local.strftime("%Y-%m-%d %H:%M %Z"))
            .replace("{schedule_name}", name))


class Scheduler:
    def __init__(self, coord: Coordinator, grace_minutes: float = 10.0):
        self.c = coord
        self.db = coord.db
        self.grace = timedelta(minutes=grace_minutes)

    # -- definitions --------------------------------------------------------------------------------
    def sync_from_config(self) -> None:
        now = now_iso()
        ids = set()
        for s in self.c.config.schedules:
            Cron(s.cron)  # validate early
            ids.add(s.id)
            existing = self.db.one("SELECT id FROM schedules WHERE id=?", (s.id,))
            if existing:
                self.db.execute("UPDATE schedules SET config_json=?, enabled=?, updated_at=? WHERE id=?",
                                (s.model_dump_json(), int(s.enabled), now, s.id))
            else:
                self.db.execute("INSERT INTO schedules(id, config_json, enabled, created_at, updated_at, last_planned_at) "
                                "VALUES (?,?,?,?,?,?)", (s.id, s.model_dump_json(), int(s.enabled), now, now, now))
        for r in self.db.query("SELECT id, config_json FROM schedules"):
            if r["id"] not in ids and loads(r["config_json"], {}).get("_source", "config") == "config":
                self.db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (r["id"],))

    def add(self, s: ScheduleConfig) -> None:
        Cron(s.cron)
        now = now_iso()
        data = {**s.model_dump(mode="json"), "_source": "owner"}
        self.db.execute(
            """INSERT INTO schedules(id, config_json, enabled, created_at, updated_at, last_planned_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json, enabled=excluded.enabled,
               updated_at=excluded.updated_at""",
            (s.id, dumps(data), int(s.enabled), now, now, now),
        )

    def get(self, sid: str) -> tuple[ScheduleConfig, dict[str, Any]] | None:
        r = self.db.one("SELECT * FROM schedules WHERE id=?", (sid,))
        if r is None:
            return None
        data = {k: v for k, v in loads(r["config_json"], {}).items() if not k.startswith("_")}
        return ScheduleConfig.model_validate(data), dict(r)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM schedules ORDER BY id"):
            cfg = {k: v for k, v in loads(r["config_json"], {}).items() if not k.startswith("_")}
            s = ScheduleConfig.model_validate(cfg)
            nxt = None
            if r["enabled"] and not r["paused"]:
                base = parse_iso(r["last_planned_at"]) or utcnow()
                nxt = iso(Cron(s.cron).next_after(max(base, utcnow()), s.timezone))
            out.append({"id": s.id, "name": s.name, "cron": s.cron, "timezone": s.timezone, "enabled": bool(r["enabled"]),
                        "paused": bool(r["paused"]), "next_run": nxt, "last_fired_at": r["last_fired_at"],
                        "last_completed_run_id": r["last_completed_run_id"]})
        return out

    def set_paused(self, sid: str, paused: bool) -> bool:
        return self.db.execute("UPDATE schedules SET paused=?, updated_at=? WHERE id=?", (int(paused), now_iso(), sid)).rowcount > 0

    def pause_all(self) -> None:
        self.db.execute("UPDATE schedules SET paused=1, updated_at=?", (now_iso(),))

    # -- firing ------------------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        interval = 0.5 if self.c.config.testing else 20.0
        while not stop.is_set():
            try:
                self.tick()
                self.update_runs()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or utcnow()
        fired: list[str] = []
        for r in self.db.query("SELECT * FROM schedules WHERE enabled=1 AND paused=0"):
            s, _ = self.get(r["id"])  # type: ignore[misc]
            cron = Cron(s.cron)
            last = parse_iso(r["last_planned_at"]) or now
            due: list[datetime] = []
            t = last
            while len(due) < 1000:
                t = cron.next_after(t, s.timezone)
                if t > now:
                    break
                due.append(t)
            if not due:
                continue
            latest = due[-1]
            for missed in due[:-1]:
                self._record(s, missed, "MISSED", "process was not running at the planned time")
            if s.missed_run_policy == "skip" and now - latest > self.grace:
                self._record(s, latest, "MISSED", "planned time passed while the process was down (policy: skip)")
            else:
                rid = self.fire(s, latest)
                if rid:
                    fired.append(rid)
            self.db.execute("UPDATE schedules SET last_planned_at=? WHERE id=?", (iso(latest), s.id))
        return fired

    def _record(self, s: ScheduleConfig, planned: datetime, status: str, note: str) -> None:
        key = f"{s.id}:{iso(planned)}"
        self.db.execute("INSERT OR IGNORE INTO schedule_runs(id, schedule_id, planned_at, dedup_key, status, note, created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (new_id("run"), s.id, iso(planned), key, status, note, now_iso()))

    def fire(self, s: ScheduleConfig, planned: datetime) -> str | None:
        key = f"{s.id}:{iso(planned)}"
        run_id = new_id("run")
        with self.db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO schedule_runs(id, schedule_id, planned_at, dedup_key, status, created_at) "
                            "VALUES (?,?,?,?,'CREATED',?)", (run_id, s.id, iso(planned), key, now_iso()))
            if cur.rowcount == 0:
                return None  # already fired for this planned time
        if s.concurrency == "forbid":
            active = [t for t in self.c.tasks.active() if t.origin == f"schedule:{s.id}"]
            paused = self.db.scalar("SELECT COUNT(*) FROM tasks WHERE origin=? AND status='PAUSED'", (f"schedule:{s.id}",))
            if active or paused:
                self.db.execute("UPDATE schedule_runs SET status='SKIPPED_OVERLAP', note=? WHERE id=?",
                                ("previous run still active", run_id))
                self.c.notifier.text(f"Schedule '{s.name}': skipped the {iso(planned)} run because the previous run is "
                                     "still active.", key=f"overlap:{key}")
                return None
        goal = render_goal(s.goal_template, planned, s.timezone, s.name)
        req = TaskRequest(goal=goal, workflow=s.workflow, sources=s.sources,
                          budget=TaskBudget(max_model_spend=s.spending_limit))
        task = self.c.submit_task(req, origin=f"schedule:{s.id}", dedup_key=key, schedule_run_id=run_id)
        self.db.execute("UPDATE schedule_runs SET task_id=? WHERE id=?", (task.id, run_id))
        self.db.execute("UPDATE schedules SET last_fired_at=? WHERE id=?", (now_iso(), s.id))
        return run_id

    def fire_external(self, sid: str, planned_at: str | None) -> str | None:
        """Trigger from an OpenClaw automation; the planned time keeps it deduplicated."""
        got = self.get(sid)
        if got is None:
            raise KeyError(sid)
        s, row = got
        if not row["enabled"] or row["paused"]:
            return None
        planned = parse_iso(planned_at) if planned_at else utcnow().replace(second=0, microsecond=0)
        return self.fire(s, planned)  # type: ignore[arg-type]

    def update_runs(self) -> None:
        rows = self.db.query("SELECT r.id, r.schedule_id, t.status FROM schedule_runs r JOIN tasks t ON t.id=r.task_id "
                             "WHERE r.status='CREATED'")
        for r in rows:
            if r["status"] in {s.value for s in TERMINAL_STATUSES}:
                status = "COMPLETED" if r["status"] in ("COMPLETED", "PARTIAL") else "FAILED"
                self.db.execute("UPDATE schedule_runs SET status=? WHERE id=?", (status, r["id"]))
                if status == "COMPLETED":
                    self.db.execute("UPDATE schedules SET last_completed_run_id=? WHERE id=?", (r["id"], r["schedule_id"]))

    def runs(self, sid: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY planned_at", (sid,))]
