"""Durable task store and lifecycle state machine (spec section 8).

Task state in this store is authoritative. Framework conversation memory should link to
task IDs rather than keep a second task database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import TERMINAL_STATUSES, ErrorCode, TaskRequest, TaskStatus, TaskView
from .db import Database, dumps, loads
from .util import new_id, now_iso

S = TaskStatus

TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.QUEUED: frozenset({S.RUNNING, S.CANCELLED, S.PAUSED, S.BLOCKED, S.FAILED}),
    S.RUNNING: frozenset({S.AWAITING_APPROVAL, S.AWAITING_USER, S.BLOCKED, S.VERIFYING, S.CANCELLED, S.PAUSED,
                          S.FAILED, S.PARTIAL, S.QUEUED}),
    S.AWAITING_APPROVAL: frozenset({S.RUNNING, S.CANCELLED, S.PAUSED, S.FAILED, S.PARTIAL, S.QUEUED}),
    S.AWAITING_USER: frozenset({S.RUNNING, S.CANCELLED, S.PAUSED, S.FAILED, S.PARTIAL, S.QUEUED}),
    S.BLOCKED: frozenset({S.RUNNING, S.QUEUED, S.CANCELLED, S.PAUSED, S.FAILED, S.PARTIAL}),
    S.PAUSED: frozenset({S.QUEUED, S.CANCELLED}),
    S.VERIFYING: frozenset({S.COMPLETED, S.PARTIAL, S.FAILED, S.RUNNING, S.CANCELLED, S.PAUSED, S.QUEUED}),
    S.COMPLETED: frozenset(),
    S.PARTIAL: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

WAITING_STATUSES = frozenset({S.AWAITING_APPROVAL, S.AWAITING_USER, S.BLOCKED})


class TransitionError(RuntimeError):
    pass


@dataclass
class TaskRecord:
    id: str
    owner: str
    goal: str
    workflow: str
    request: TaskRequest
    status: TaskStatus
    phase: str | None
    plan: Any
    current_step: str | None
    worker_id: str | None
    limits: dict[str, Any]
    blocking_reason: str | None
    error_code: str | None
    result: dict[str, Any] | None
    checkpoint_version: int
    action_count: int
    active_ms: int
    origin: str
    schedule_run_id: str | None
    status_message_ref: str | None
    last_progress_at: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _row(r: Any) -> TaskRecord:
    return TaskRecord(
        id=r["id"],
        owner=r["owner"],
        goal=r["goal"],
        workflow=r["workflow"],
        request=TaskRequest.model_validate(loads(r["request_json"])),
        status=TaskStatus(r["status"]),
        phase=r["phase"],
        plan=loads(r["plan_json"]),
        current_step=r["current_step"],
        worker_id=r["worker_id"],
        limits=loads(r["limits_json"], {}),
        blocking_reason=r["blocking_reason"],
        error_code=r["error_code"],
        result=loads(r["result_json"]),
        checkpoint_version=r["checkpoint_version"],
        action_count=r["action_count"],
        active_ms=r["active_ms"],
        origin=r["origin"],
        schedule_run_id=r["schedule_run_id"],
        status_message_ref=r["status_message_ref"],
        last_progress_at=r["last_progress_at"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        finished_at=r["finished_at"],
    )


class TaskStore:
    def __init__(self, db: Database):
        self.db = db

    # -- creation -----------------------------------------------------------
    def create(
        self,
        request: TaskRequest,
        *,
        owner: str,
        workflow: str,
        limits: dict[str, Any],
        origin: str = "owner",
        dedup_key: str | None = None,
        schedule_run_id: str | None = None,
    ) -> TaskRecord:
        task_id = new_id("tsk")
        now = now_iso()
        with self.db.tx() as c:
            if dedup_key:
                existing = c.execute("SELECT id FROM tasks WHERE dedup_key=?", (dedup_key,)).fetchone()
                if existing:
                    return self.get(existing["id"])  # type: ignore[return-value]
            c.execute(
                """INSERT INTO tasks(id, owner, goal, workflow, request_json, status, limits_json, origin, dedup_key,
                   schedule_run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, owner, request.goal, workflow, request.model_dump_json(), S.QUEUED.value, dumps(limits),
                 origin, dedup_key, schedule_run_id, now, now),
            )
            self.event(task_id, "created", {"workflow": workflow, "origin": origin, "goal": request.goal})
        return self.get(task_id)  # type: ignore[return-value]

    # -- reads --------------------------------------------------------------
    def get(self, task_id: str) -> TaskRecord | None:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return _row(r) if r else None

    def require(self, task_id: str) -> TaskRecord:
        t = self.get(task_id)
        if t is None:
            raise KeyError(f"unknown task {task_id}")
        return t

    def list(self, statuses: list[TaskStatus] | None = None, limit: int = 50) -> list[TaskRecord]:
        if statuses:
            q = ",".join("?" for _ in statuses)
            rows = self.db.query(
                f"SELECT * FROM tasks WHERE status IN ({q}) ORDER BY created_at DESC LIMIT ?",
                (*[s.value for s in statuses], limit),
            )
        else:
            rows = self.db.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]

    def active(self) -> list[TaskRecord]:
        return self.list([s for s in TaskStatus if s not in TERMINAL_STATUSES and s != S.PAUSED], limit=1000)

    def by_short_id(self, fragment: str) -> TaskRecord | None:
        fragment = fragment.strip()
        if fragment.startswith("tsk_"):
            return self.get(fragment)
        rows = self.db.query("SELECT id FROM tasks WHERE id LIKE ? ORDER BY created_at DESC LIMIT 2", (f"%{fragment.upper()}",))
        if len(rows) == 1:
            return self.get(rows[0]["id"])
        return None

    # -- lifecycle ----------------------------------------------------------
    def transition(
        self,
        task_id: str,
        to: TaskStatus,
        *,
        reason: str | None = None,
        error_code: ErrorCode | str | None = None,
        result: dict[str, Any] | None = None,
        allow_from: set[TaskStatus] | None = None,
    ) -> TaskRecord:
        with self.db.tx() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskStatus(row["status"])
            if current == to:
                if reason is not None:
                    c.execute("UPDATE tasks SET blocking_reason=?, updated_at=? WHERE id=?", (reason, now_iso(), task_id))
                return self.require(task_id)
            if allow_from is not None and current not in allow_from:
                raise TransitionError(f"task {task_id} is {current}, expected one of {sorted(allow_from)}")
            if to not in TRANSITIONS[current]:
                raise TransitionError(f"illegal transition {current} -> {to} for task {task_id}")
            now = now_iso()
            finished = now if to in TERMINAL_STATUSES else None
            blocking = reason if to in WAITING_STATUSES | {S.PAUSED} else None
            sets = ["status=?", "updated_at=?", "checkpoint_version=checkpoint_version+1", "blocking_reason=?"]
            params: list[Any] = [to.value, now, blocking]
            if finished:
                sets.append("finished_at=?")
                params.append(finished)
            if error_code is not None:
                sets.append("error_code=?")
                params.append(str(error_code))
            if result is not None:
                sets.append("result_json=?")
                params.append(dumps(result))
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*params, task_id))
            self.event(task_id, "status", {"from": current.value, "to": to.value, "reason": reason,
                                           "error_code": str(error_code) if error_code else None})
        return self.require(task_id)

    def update(self, task_id: str, **fields: Any) -> None:
        allowed = {"phase", "plan", "current_step", "worker_id", "status_message_ref", "last_progress_at", "result",
                   "workflow", "goal", "request"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            col = {"plan": "plan_json", "result": "result_json", "request": "request_json"}.get(k, k)
            if k in ("plan", "result"):
                v = dumps(v)
            if k == "request":
                v = v.model_dump_json()
            sets.append(f"{col}=?")
            params.append(v)
        sets.append("updated_at=?")
        params.append(now_iso())
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*params, task_id))

    def add_action_count(self, task_id: str, n: int = 1) -> int:
        with self.db.tx() as c:
            c.execute("UPDATE tasks SET action_count=action_count+? WHERE id=?", (n, task_id))
            return c.execute("SELECT action_count FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    def add_active_ms(self, task_id: str, ms: int) -> int:
        with self.db.tx() as c:
            c.execute("UPDATE tasks SET active_ms=active_ms+? WHERE id=?", (max(0, ms), task_id))
            return c.execute("SELECT active_ms FROM tasks WHERE id=?", (task_id,)).fetchone()[0]

    # -- events / audit -------------------------------------------------------
    def event(self, task_id: str | None, kind: str, data: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO task_events(task_id, ts, kind, data_json) VALUES (?,?,?,?)",
            (task_id, now_iso(), kind, dumps(data or {})),
        )

    def events(self, task_id: str, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,))
        out = [{"ts": r["ts"], "kind": r["kind"], **loads(r["data_json"], {})} for r in rows]
        return [e for e in out if not kinds or e["kind"] in kinds]

    # -- owner messages (steering / answers) ----------------------------------
    def add_message(self, task_id: str, kind: str, text: str) -> str:
        mid = new_id("msg")
        self.db.execute(
            "INSERT INTO task_messages(id, task_id, kind, text, created_at) VALUES (?,?,?,?,?)",
            (mid, task_id, kind, text, now_iso()),
        )
        self.event(task_id, f"owner_{kind}", {"text": text})
        return mid

    def take_messages(self, task_id: str, kind: str) -> list[str]:
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT id, text FROM task_messages WHERE task_id=? AND kind=? AND consumed=0 ORDER BY created_at",
                (task_id, kind),
            ).fetchall()
            for r in rows:
                c.execute("UPDATE task_messages SET consumed=1 WHERE id=?", (r["id"],))
        return [r["text"] for r in rows]

    def steering(self, task_id: str) -> list[str]:
        """All steering instructions so far (constraints persist across steps)."""
        rows = self.db.query("SELECT text FROM task_messages WHERE task_id=? AND kind='steer' ORDER BY created_at", (task_id,))
        return [r["text"] for r in rows]

    # -- views ----------------------------------------------------------------
    def view(self, task: TaskRecord, artifacts: list[Any] | None = None, progress: str | None = None) -> TaskView:
        pending = self.db.scalar(
            "SELECT id FROM approvals WHERE task_id=? AND status='PENDING' ORDER BY created_at DESC LIMIT 1", (task.id,)
        )
        return TaskView(
            task_id=task.id,
            status=task.status,
            workflow=task.workflow,
            goal=task.goal,
            phase=task.phase,
            current_step=task.current_step,
            worker_id=task.worker_id,
            blocking_reason=task.blocking_reason,
            progress=progress,
            created_at=task.created_at,
            updated_at=task.updated_at,
            result=task.result,
            artifacts=artifacts or [],
            pending_approval_id=pending,
        )
