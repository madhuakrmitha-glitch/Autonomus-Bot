"""Worker manager: registration, capability routing, leases with fencing tokens, job dispatch
and receipts (spec sections 7, 8 and 16; FR22, FR23, FR25).

* A lease grants one task exclusive use of a resource (e.g. ``vm1:ui``) and carries a
  monotonically increasing fencing token. A newer lease always has a larger token.
* Jobs carry the token; workers refuse tokens older than the newest they have seen, and the
  coordinator refuses receipts whose token is no longer current.
* A worker process registers with a fresh ``session_epoch``; jobs claimed by an older epoch
  are fenced out and their side effects are reconciled before further work.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .config import ApplicationConfig
from .contracts import (
    ActionReceipt,
    ErrorCode,
    ReceiptError,
    ReceiptStatus,
    WorkerHeartbeat,
    WorkerJob,
    WorkerRegistration,
)
from .db import Database, dumps, loads
from .policy.operations import OPERATIONS
from .secrets import resolve
from .util import iso, new_id, now_iso, parse_iso, utcnow


class WorkerError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Lease:
    lease_id: str
    resource: str
    task_id: str
    worker_id: str
    fencing_token: int
    expires_at: str


# Output kinds a job may upload; anything else is refused (FR25: only declared outputs transfer).
OP_OUTPUTS: dict[str, list[str]] = {
    "browser.observe": ["screenshot"],
    "browser.agent_task": ["screenshot"],
    "browser.read_page": ["screenshot"],
    "browser.click": ["screenshot"],
    "browser.click_point": ["screenshot"],
    "browser.type": ["screenshot"],
    "browser.press": ["screenshot"],
    "browser.select": ["screenshot"],
    "browser.scroll": ["screenshot"],
    "browser.navigate": ["screenshot"],
    "browser.back": ["screenshot"],
    "browser.wait": ["screenshot"],
    "browser.fetch_image": ["image"],
    "browser.download": ["download", "screenshot"],
    "browser.video_frames": ["frame"],
    "media.fetch": ["media", "subtitles"],
    "email.draft": ["draft"],
    "calendar.draft": ["draft"],
    "spreadsheet.write": ["workbook"],
}


class WorkerManager:
    def __init__(self, db: Database, config: ApplicationConfig):
        self.db = db
        self.config = config
        self._waiters: dict[str, asyncio.Future[ActionReceipt]] = {}
        self._job_events: dict[str, asyncio.Event] = {}
        self._token_hashes: dict[str, str] = {}

    # -- authentication ------------------------------------------------------------------
    def authenticate(self, worker_id: str, token: str | None) -> bool:
        entry = self.config.worker(worker_id)
        if entry is None or not entry.enabled or not token:
            return False
        expected = resolve(entry.credential_ref)
        if not expected:
            return False
        return hmac.compare_digest(hashlib.sha256(expected.encode()).digest(), hashlib.sha256(token.encode()).digest())

    # -- registration and health ------------------------------------------------------------
    def register(self, reg: WorkerRegistration) -> dict[str, Any]:
        entry = self.config.worker(reg.worker_id)
        if entry is None or not entry.enabled:
            raise WorkerError(ErrorCode.AUTH_REQUIRED, f"worker {reg.worker_id} is not enrolled")
        caps = sorted(set(reg.capabilities) & set(entry.capabilities))
        now = now_iso()
        with self.db.tx() as c:
            prev = c.execute("SELECT session_epoch FROM workers WHERE id=?", (reg.worker_id,)).fetchone()
            c.execute(
                """INSERT INTO workers(id, capabilities_json, session_epoch, version, platform, resources_json,
                   attachments_json, status, last_heartbeat, registered_at) VALUES (?,?,?,?,?,?,?,'ONLINE',?,?)
                   ON CONFLICT(id) DO UPDATE SET capabilities_json=excluded.capabilities_json,
                   session_epoch=excluded.session_epoch, version=excluded.version, platform=excluded.platform,
                   resources_json=excluded.resources_json, attachments_json=excluded.attachments_json,
                   status='ONLINE', last_heartbeat=excluded.last_heartbeat""",
                (reg.worker_id, dumps(caps), reg.session_epoch, reg.version, reg.platform,
                 dumps(reg.interactive_resources), dumps(reg.attachments), now, now),
            )
            if prev and prev["session_epoch"] and prev["session_epoch"] != reg.session_epoch:
                # jobs held by the previous process can no longer report; their effects need reconciliation
                rows = c.execute("SELECT id FROM jobs WHERE worker_id=? AND status='CLAIMED'", (reg.worker_id,)).fetchall()
                for r in rows:
                    self._finish_job(c, r["id"], self._unknown_receipt(r["id"], "worker process restarted mid-job"))
        return {"worker_id": reg.worker_id, "capabilities": caps, "server_time": now,
                "heartbeat_seconds": self.config.limits.worker_heartbeat_seconds,
                "lease_ttl_seconds": self.config.limits.lease_ttl_seconds}

    def _check_epoch(self, worker_id: str, epoch: str) -> None:
        current = self.db.scalar("SELECT session_epoch FROM workers WHERE id=?", (worker_id,))
        if current != epoch:
            raise WorkerError(ErrorCode.STALE_FENCING_TOKEN, "stale worker session; register again")

    def heartbeat(self, worker_id: str, hb: WorkerHeartbeat) -> dict[str, Any]:
        self._check_epoch(worker_id, hb.session_epoch)
        self.db.execute("UPDATE workers SET last_heartbeat=?, status='ONLINE', health_json=? WHERE id=?",
                        (now_iso(), dumps(hb.health), worker_id))
        tokens = {r["name"]: r["fencing_counter"] for r in self.db.query("SELECT * FROM resources WHERE name LIKE ?",
                                                                          (f"{worker_id}:%",))}
        cancelled = [r["id"] for r in self.db.query(
            "SELECT id FROM jobs WHERE worker_id=? AND status='CLAIMED' AND cancel_requested=1", (worker_id,))]
        return {"ok": True, "server_time": now_iso(), "fencing_tokens": tokens, "cancel_job_ids": cancelled}

    def mark_offline_workers(self) -> list[str]:
        cutoff = iso(utcnow() - timedelta(seconds=self.config.limits.lease_ttl_seconds))
        rows = self.db.query("SELECT id FROM workers WHERE status='ONLINE' AND last_heartbeat < ?", (cutoff,))
        for r in rows:
            self.db.execute("UPDATE workers SET status='OFFLINE' WHERE id=?", (r["id"],))
        return [r["id"] for r in rows]

    def workers(self) -> list[dict[str, Any]]:
        return [{"worker_id": r["id"], "status": r["status"], "capabilities": loads(r["capabilities_json"], []),
                 "last_heartbeat": r["last_heartbeat"], "version": r["version"], "platform": r["platform"],
                 "attachments": loads(r["attachments_json"], [])}
                for r in self.db.query("SELECT * FROM workers ORDER BY id")]

    def route(self, capability: str | None, preferred: str | None = None) -> str:
        """Pick an online worker with the capability, preferring an explicitly routed one, then idle ones."""
        self.mark_offline_workers()
        rows = self.db.query("SELECT * FROM workers WHERE status='ONLINE' ORDER BY id")
        candidates = [r for r in rows if capability is None or capability in loads(r["capabilities_json"], [])]
        if preferred:
            match = [r for r in candidates if r["id"] == preferred]
            if not match:
                raise WorkerError(ErrorCode.WORKER_UNAVAILABLE,
                                  f"worker {preferred} is offline or lacks the {capability} capability")
            return preferred
        if not candidates:
            raise WorkerError(ErrorCode.WORKER_UNAVAILABLE, f"no online worker has the {capability} capability")

        def busy(wid: str) -> int:
            return self.db.scalar(
                "SELECT COUNT(*) FROM leases WHERE worker_id=? AND status='ACTIVE' AND expires_at > ?", (wid, now_iso())
            ) or 0

        return min(candidates, key=lambda r: (busy(r["id"]), r["id"]))["id"]

    # -- leases ---------------------------------------------------------------------------------
    def acquire_lease(self, task_id: str, worker_id: str, resource: str) -> Lease:
        ttl = self.config.limits.lease_ttl_seconds
        now = utcnow()
        with self.db.tx() as c:
            c.execute("UPDATE leases SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at <= ?", (iso(now),))
            active = c.execute("SELECT * FROM leases WHERE resource=? AND status='ACTIVE'", (resource,)).fetchone()
            if active is not None:
                if active["task_id"] == task_id and active["worker_id"] == worker_id:
                    exp = iso(now + timedelta(seconds=ttl))
                    c.execute("UPDATE leases SET expires_at=? WHERE id=?", (exp, active["id"]))
                    return Lease(active["id"], resource, task_id, worker_id, active["fencing_token"], exp)  # type: ignore[arg-type]
                raise WorkerError(ErrorCode.WORKER_UNAVAILABLE,
                                  f"{resource} is leased to task {active['task_id']} until {active['expires_at']}")
            c.execute("INSERT OR IGNORE INTO resources(name, fencing_counter) VALUES (?, 0)", (resource,))
            c.execute("UPDATE resources SET fencing_counter=fencing_counter+1 WHERE name=?", (resource,))
            token = c.execute("SELECT fencing_counter FROM resources WHERE name=?", (resource,)).fetchone()[0]
            lease_id = new_id("lse")
            exp = iso(now + timedelta(seconds=ttl))
            c.execute(
                """INSERT INTO leases(id, resource, task_id, worker_id, fencing_token, status, acquired_at, expires_at)
                   VALUES (?,?,?,?,?,'ACTIVE',?,?)""",
                (lease_id, resource, task_id, worker_id, token, iso(now), exp),
            )
        return Lease(lease_id, resource, task_id, worker_id, token, exp)  # type: ignore[arg-type]

    def renew_lease(self, lease_id: str) -> Lease | None:
        exp = iso(utcnow() + timedelta(seconds=self.config.limits.lease_ttl_seconds))
        with self.db.tx() as c:
            cur = c.execute("UPDATE leases SET expires_at=? WHERE id=? AND status='ACTIVE' AND expires_at > ?",
                            (exp, lease_id, now_iso()))
            if cur.rowcount != 1:
                return None
            r = c.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        return Lease(r["id"], r["resource"], r["task_id"], r["worker_id"], r["fencing_token"], r["expires_at"])

    def release_lease(self, lease_id: str) -> None:
        self.db.execute("UPDATE leases SET status='RELEASED', released_at=? WHERE id=? AND status='ACTIVE'",
                        (now_iso(), lease_id))

    def release_task_leases(self, task_id: str) -> None:
        self.db.execute("UPDATE leases SET status='RELEASED', released_at=? WHERE task_id=? AND status='ACTIVE'",
                        (now_iso(), task_id))

    def current_token(self, resource: str) -> int:
        return self.db.scalar("SELECT fencing_counter FROM resources WHERE name=?", (resource,)) or 0

    def lease_holder(self, resource: str) -> str | None:
        return self.db.scalar("SELECT task_id FROM leases WHERE resource=? AND status='ACTIVE' AND expires_at > ?",
                              (resource, now_iso()))

    # -- jobs -----------------------------------------------------------------------------------
    def create_job(self, job: WorkerJob) -> WorkerJob:
        now = now_iso()
        with self.db.tx() as c:
            existing = c.execute("SELECT job_json FROM jobs WHERE action_id=?", (job.action_id,)).fetchone()
            if existing:
                return WorkerJob.model_validate(loads(existing["job_json"]))
            c.execute(
                """INSERT INTO jobs(id, task_id, action_id, worker_id, lease_id, fencing_token, operation, job_json,
                   status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,'PENDING',?,?)""",
                (job.job_id, job.task_id, job.action_id, job.worker_id, job.lease_id, job.fencing_token, job.operation,
                 job.model_dump_json(), now, now),
            )
        self._job_events.setdefault(job.worker_id, asyncio.Event()).set()
        return job

    async def dispatch(self, job: WorkerJob, timeout: float) -> ActionReceipt:
        job = self.create_job(job)
        existing = self.receipt_for_action(job.action_id)
        if existing is not None:
            return existing
        loop = asyncio.get_running_loop()
        fut = self._waiters.get(job.job_id)
        if fut is None or fut.done():
            fut = loop.create_future()
            self._waiters[job.job_id] = fut
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except TimeoutError:
            receipt = self._unknown_receipt(job.job_id, "no receipt before the job deadline")
            with self.db.tx() as c:
                status = c.execute("SELECT status FROM jobs WHERE id=?", (job.job_id,)).fetchone()["status"]
                if status == "PENDING":
                    # never claimed: nothing ran, so this is a clean failure, not an uncertain effect
                    receipt = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=job.worker_id,
                                            fencing_token=job.fencing_token, status=ReceiptStatus.FAILED,
                                            started_at=now_iso(), finished_at=now_iso(),
                                            error=ReceiptError(code=ErrorCode.WORKER_UNAVAILABLE,
                                                               message="worker did not claim the job", retryable=True))
                    self._finish_job(c, job.job_id, receipt, status="EXPIRED")
                elif status == "CLAIMED":
                    self._finish_job(c, job.job_id, receipt, status="EXPIRED")
                else:
                    stored = self.receipt_for_action(job.action_id)
                    if stored:
                        receipt = stored
            return receipt
        finally:
            self._waiters.pop(job.job_id, None)

    def cancel_job(self, job_id: str) -> None:
        """Cancel a job.

        An unclaimed job is finished as a clean failure (nothing ran). A claimed read-only job is
        interrupted through the worker's next heartbeat. A claimed mutation is left to finish, because
        interrupting it would leave its effect uncertain; its receipt is recorded as usual.
        """
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None:
                return
            if r["status"] == "CLAIMED":
                spec = OPERATIONS.get(r["operation"])
                if spec is not None and spec.retry_safe:
                    c.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now_iso(), job_id))
                return
            if r["status"] != "PENDING":
                return
            now = now_iso()
            rec = ActionReceipt(action_id=r["action_id"], job_id=job_id, worker_id=r["worker_id"],
                                fencing_token=r["fencing_token"], status=ReceiptStatus.FAILED, started_at=now,
                                finished_at=now, error=ReceiptError(code=ErrorCode.CANCELLED,
                                                                    message="cancelled before a worker claimed it"))
            self._finish_job(c, job_id, rec, status="CANCELLED")

    def supersede_job(self, job_id: str) -> None:
        """Detach a finished job from its action so a read-only action can be executed again."""
        self.db.execute("UPDATE jobs SET action_id = action_id || ':superseded:' || id WHERE id=? AND status IN "
                        "('DONE','EXPIRED','CANCELLED')", (job_id,))

    def claim(self, worker_id: str, epoch: str) -> WorkerJob | None:
        now = now_iso()
        with self.db.tx() as c:
            # checked in the same transaction as the claim, so a re-registration cannot slip in between
            live = c.execute("SELECT session_epoch FROM workers WHERE id=?", (worker_id,)).fetchone()
            if live is None or live[0] != epoch:
                raise WorkerError(ErrorCode.STALE_FENCING_TOKEN, "stale worker session; register again")
            rows = c.execute("SELECT * FROM jobs WHERE worker_id=? AND status='PENDING' ORDER BY created_at",
                             (worker_id,)).fetchall()
            for r in rows:
                job = WorkerJob.model_validate(loads(r["job_json"]))
                lease = c.execute("SELECT * FROM leases WHERE id=?", (job.lease_id,)).fetchone()
                current = c.execute("SELECT fencing_counter FROM resources WHERE name=?", (job.resource,)).fetchone()
                valid = (lease is not None and lease["status"] == "ACTIVE" and lease["expires_at"] > now
                         and current is not None and current[0] == job.fencing_token and job.deadline > now)
                if not valid:
                    rec = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=worker_id,
                                        fencing_token=job.fencing_token, status=ReceiptStatus.REJECTED,
                                        started_at=now, finished_at=now,
                                        error=ReceiptError(code=ErrorCode.STALE_FENCING_TOKEN,
                                                           message="lease expired or superseded before the job was claimed"))
                    self._finish_job(c, job.job_id, rec, status="EXPIRED")
                    continue
                c.execute("UPDATE jobs SET status='CLAIMED', claimed_epoch=?, claimed_at=?, updated_at=? WHERE id=?",
                          (epoch, now, now, job.job_id))
                return job
        return None

    async def claim_wait(self, worker_id: str, epoch: str, wait_seconds: float) -> WorkerJob | None:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        ev = self._job_events.setdefault(worker_id, asyncio.Event())
        while True:
            job = self.claim(worker_id, epoch)
            if job is not None:
                return job
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(remaining, 1.0))
            except TimeoutError:
                pass

    def submit_receipt(self, worker_id: str, epoch: str, receipt: ActionReceipt) -> dict[str, Any]:
        """Store a receipt. Stale receipts are still recorded (the effect may be real) but flagged."""
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (receipt.job_id,)).fetchone()
            if r is None or r["worker_id"] != worker_id or r["action_id"] != receipt.action_id:
                raise WorkerError(ErrorCode.AUTH_REQUIRED, "receipt does not match a job for this worker")
            job = WorkerJob.model_validate(loads(r["job_json"]))
            current = c.execute("SELECT fencing_counter FROM resources WHERE name=?", (job.resource,)).fetchone()
            live_epoch = c.execute("SELECT session_epoch FROM workers WHERE id=?", (worker_id,)).fetchone()
            stale = (receipt.fencing_token != job.fencing_token or current is None or current[0] != job.fencing_token
                     or r["claimed_epoch"] != epoch or live_epoch is None or live_epoch[0] != epoch)
            existing = ActionReceipt.model_validate_json(r["receipt_json"]) if r["receipt_json"] else None
            uncertain = existing is not None and existing.status == ReceiptStatus.UNKNOWN
            if existing is not None and r["status"] == "DONE" and not uncertain:
                return {"accepted": False, "duplicate": True}
            if stale:
                c.execute(
                    "INSERT INTO task_events(task_id, ts, kind, data_json) VALUES (?,?,?,?)",
                    (job.task_id, now_iso(), "stale_receipt",
                     dumps({"job_id": job.job_id, "action_id": job.action_id, "status": receipt.status.value,
                            "token": receipt.fencing_token, "current": current[0] if current else None})),
                )
                if r["status"] in ("CLAIMED", "EXPIRED") or uncertain:
                    # a late, authenticated outcome replaces "unknown" so reconciliation never repeats a real effect
                    c.execute("UPDATE jobs SET receipt_json=?, updated_at=? WHERE id=?",
                              (receipt.model_dump_json(), now_iso(), job.job_id))
                return {"accepted": False, "stale": True}
            self._finish_job(c, job.job_id, receipt)
        return {"accepted": True}

    def _finish_job(self, c: Any, job_id: str, receipt: ActionReceipt, status: str = "DONE") -> None:
        c.execute("UPDATE jobs SET status=?, receipt_json=?, updated_at=? WHERE id=?",
                  (status, receipt.model_dump_json(), now_iso(), job_id))
        fut = self._waiters.get(job_id)
        if fut is not None and not fut.done():
            fut.get_loop().call_soon_threadsafe(_set_result, fut, receipt)

    def _unknown_receipt(self, job_id: str, why: str) -> ActionReceipt:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        now = now_iso()
        return ActionReceipt(action_id=r["action_id"] if r else "", job_id=job_id,
                             worker_id=r["worker_id"] if r else None,
                             fencing_token=r["fencing_token"] if r else None, status=ReceiptStatus.UNKNOWN,
                             started_at=now, finished_at=now,
                             error=ReceiptError(code=ErrorCode.UNCERTAIN_SIDE_EFFECT, message=why))

    def receipt_for_action(self, action_id: str) -> ActionReceipt | None:
        r = self.db.one("SELECT receipt_json, status FROM jobs WHERE action_id=?", (action_id,))
        if r is None or not r["receipt_json"] or r["status"] not in ("DONE", "EXPIRED"):
            return None
        return ActionReceipt.model_validate_json(r["receipt_json"])

    def job_for_action(self, action_id: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM jobs WHERE action_id=?", (action_id,))
        return dict(r) if r else None

    def validate_upload(self, worker_id: str, job_id: str, output_kind: str) -> WorkerJob:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if r is None or r["worker_id"] != worker_id or r["status"] != "CLAIMED":
            raise WorkerError(ErrorCode.AUTH_REQUIRED, "uploads must belong to a job this worker is executing")
        job = WorkerJob.model_validate(loads(r["job_json"]))
        allowed = job.declared_outputs or OP_OUTPUTS.get(job.operation, [])
        if output_kind not in allowed:
            raise WorkerError(ErrorCode.POLICY_DENIED, f"{output_kind} is not a declared output of {job.operation}")
        return job

    def validate_input_download(self, worker_id: str, job_id: str, artifact_id: str) -> WorkerJob:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if r is None or r["worker_id"] != worker_id or r["status"] != "CLAIMED":
            raise WorkerError(ErrorCode.AUTH_REQUIRED, "inputs are only served to the worker executing the job")
        job = WorkerJob.model_validate(loads(r["job_json"]))
        if artifact_id not in job.declared_inputs:
            raise WorkerError(ErrorCode.POLICY_DENIED, f"artifact {artifact_id} is not a declared input of this job")
        return job

    def open_jobs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM jobs WHERE status IN ('PENDING','CLAIMED')")]


def _set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def job_deadline(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))  # type: ignore[return-value]


def lease_expired(lease: Lease) -> bool:
    exp = parse_iso(lease.expires_at)
    return exp is None or exp <= utcnow()
