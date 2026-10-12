"""Action-bound owner approvals (spec section 15).

An approval binds owner, task, action digest, destination, preconditions, policy version,
expiry and a one-use nonce. The binding is HMAC-signed; the nonce is consumed atomically.
Before execution the coordinator revalidates the current target: changed inputs, a
changed target state, a different policy version or an expired decision invalidate it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..config import ApplicationConfig
from ..contracts import ActionIntent, ApprovalRequest, ApprovalStatus, ErrorCode
from ..db import Database, dumps, loads
from ..secrets import resolve
from ..util import (
    canonical_json,
    digest,
    hmac_sign,
    hmac_verify,
    iso,
    new_id,
    now_iso,
    parse_iso,
    random_token,
    utcnow,
)


class ApprovalError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Identity:
    """An authenticated decider. ``channel`` and ``user_id`` come from the channel layer, never a tool payload."""

    channel: str
    user_id: str


def binding_view(intent: ActionIntent, fingerprint: str | None, destination: str | None) -> dict[str, Any]:
    """Stable part of an intent: element refs are ephemeral, so they are replaced by the target fingerprint."""
    args = {k: v for k, v in intent.arguments.items() if k != "ref"}
    return {
        "task_id": intent.task_id,
        "action_id": intent.action_id,
        "operation": intent.operation,
        "arguments": args,
        "target_fingerprint": fingerprint,
        "destination": destination,
    }


def load_signing_key(config: ApplicationConfig) -> bytes:
    value = resolve(config.server.approval_signing_key_ref)
    if value:
        return value.encode("utf-8")
    path = Path(config.storage.data_dir) / "secrets" / "approval_signing.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(random_token(48))
    return path.read_text(encoding="utf-8").strip().encode("utf-8")


class ApprovalService:
    def __init__(self, db: Database, config: ApplicationConfig, signing_key: bytes | None = None):
        self.db = db
        self.config = config
        self.key = signing_key or load_signing_key(config)
        self._events: dict[str, asyncio.Event] = {}

    # -- creation -------------------------------------------------------------
    def create(
        self,
        intent: ActionIntent,
        *,
        owner: str,
        policy_version: str,
        destination: str | None,
        consequence: str,
        preview: str,
        preconditions: dict[str, Any],
        fingerprint: str | None,
    ) -> ApprovalRequest:
        approval_id = new_id("apr")
        nonce = random_token(16)
        expires = iso(utcnow() + timedelta(minutes=self.config.limits.approval_expiry_minutes))
        action_digest = digest(binding_view(intent, fingerprint, destination))
        signature = self._sign(owner, intent.task_id, action_digest, destination, preconditions, policy_version,
                               expires, nonce)  # type: ignore[arg-type]
        now = now_iso()
        self.db.execute(
            """INSERT INTO approvals(id, task_id, action_id, owner, operation, action_digest, destination, consequence,
               preview, preconditions_json, policy_version, nonce, signature, status, expires_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (approval_id, intent.task_id, intent.action_id, owner, intent.operation, action_digest, destination,
             consequence, preview, dumps(preconditions), policy_version, nonce, signature,
             ApprovalStatus.PENDING.value, expires, now),
        )
        return self.get(approval_id)  # type: ignore[return-value]

    def _sign(self, owner: str, task_id: str, action_digest: str, destination: str | None,
              preconditions: dict[str, Any], policy_version: str, expires: str, nonce: str) -> str:
        message = canonical_json({
            "owner": owner, "task": task_id, "digest": action_digest, "destination": destination,
            "preconditions": preconditions, "policy": policy_version, "expires": expires, "nonce": nonce,
        })
        return hmac_sign(self.key, message)

    # -- reads -----------------------------------------------------------------
    def get(self, approval_id: str) -> ApprovalRequest | None:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if r is None:
            return None
        return ApprovalRequest(
            approval_id=r["id"], task_id=r["task_id"], action_id=r["action_id"], owner=r["owner"],
            operation=r["operation"], action_digest=r["action_digest"], destination=r["destination"],
            consequence=r["consequence"], preview=r["preview"], preconditions=loads(r["preconditions_json"], {}),
            policy_version=r["policy_version"], expires_at=r["expires_at"], status=ApprovalStatus(r["status"]),
        )

    def for_action(self, action_id: str) -> ApprovalRequest | None:
        aid = self.db.scalar("SELECT id FROM approvals WHERE action_id=? ORDER BY created_at DESC LIMIT 1", (action_id,))
        return self.get(aid) if aid else None

    def set_message_ref(self, approval_id: str, ref: str) -> None:
        self.db.execute("UPDATE approvals SET message_ref=? WHERE id=?", (ref, approval_id))

    def message_ref(self, approval_id: str) -> str | None:
        return self.db.scalar("SELECT message_ref FROM approvals WHERE id=?", (approval_id,))

    # -- decisions -------------------------------------------------------------
    def decide(self, approval_id: str, identity: Identity, decision: str, note: str | None = None) -> ApprovalRequest:
        if identity.channel == "telegram":
            allowed = {str(u) for u in self.config.owner.approvers}
            if identity.user_id not in allowed:
                raise ApprovalError(ErrorCode.AUTH_REQUIRED, "this identity is not an authorized approver")
        elif identity.channel != "owner_api":
            raise ApprovalError(ErrorCode.AUTH_REQUIRED, f"approvals cannot be decided through {identity.channel}")
        if decision not in {"approve", "deny"}:
            raise ApprovalError(ErrorCode.MALFORMED_TOOL_CALL, "decision must be approve or deny")
        error: ApprovalError | None = None
        with self.db.tx() as c:
            r = c.execute("SELECT status, expires_at FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                error = ApprovalError(ErrorCode.APPROVAL_INVALID, "unknown approval")
            elif r["status"] != ApprovalStatus.PENDING.value:
                error = ApprovalError(ErrorCode.APPROVAL_INVALID, f"approval is already {r['status'].lower()}")
            elif parse_iso(r["expires_at"]) <= utcnow():  # type: ignore[operator]
                c.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?", (approval_id,))
                error = ApprovalError(ErrorCode.APPROVAL_EXPIRED, "approval expired before a decision was made")
            else:
                status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.DENIED
                c.execute(
                    "UPDATE approvals SET status=?, decided_at=?, decided_by=?, decision_note=? WHERE id=? AND status='PENDING'",
                    (status.value, now_iso(), f"{identity.channel}:{identity.user_id}", note, approval_id),
                )
        self._notify(approval_id)
        if error is not None:
            raise error
        return self.get(approval_id)  # type: ignore[return-value]

    def expire_due(self) -> list[str]:
        now = now_iso()
        rows = self.db.query("SELECT id FROM approvals WHERE status='PENDING' AND expires_at <= ?", (now,))
        for r in rows:
            self.db.execute("UPDATE approvals SET status='EXPIRED' WHERE id=? AND status='PENDING'", (r["id"],))
            self._notify(r["id"])
        return [r["id"] for r in rows]

    def invalidate_task(self, task_id: str, reason: str) -> None:
        rows = self.db.query("SELECT id FROM approvals WHERE task_id=? AND status IN ('PENDING','APPROVED')", (task_id,))
        for r in rows:
            self.db.execute("UPDATE approvals SET status='INVALIDATED', decision_note=? WHERE id=?", (reason, r["id"]))
            self._notify(r["id"])

    # -- waiting ----------------------------------------------------------------
    def _notify(self, approval_id: str) -> None:
        ev = self._events.get(approval_id)
        if ev is not None:
            ev.set()

    async def wait(self, approval_id: str, poll_seconds: float = 5.0) -> ApprovalRequest:
        ev = self._events.setdefault(approval_id, asyncio.Event())
        try:
            while True:
                self.expire_due()
                ap = self.get(approval_id)
                if ap is None:
                    raise ApprovalError(ErrorCode.APPROVAL_INVALID, "approval disappeared")
                if ap.status != ApprovalStatus.PENDING:
                    return ap
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
        finally:
            self._events.pop(approval_id, None)

    # -- consumption --------------------------------------------------------------
    def consume(
        self,
        approval_id: str,
        intent: ActionIntent,
        *,
        owner: str,
        fingerprint: str | None,
        destination: str | None,
        current_preconditions: dict[str, Any],
        current_policy_version: str,
    ) -> None:
        """Validate the binding against the *current* intent and state, then consume the nonce once.

        Status changes (expired, invalidated) are committed before the error is raised.
        """
        error: ApprovalError | None = None
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                raise ApprovalError(ErrorCode.APPROVAL_INVALID, "unknown approval")
            error = self._check_binding(c, r, intent, owner=owner, fingerprint=fingerprint, destination=destination,
                                        current_preconditions=current_preconditions,
                                        current_policy_version=current_policy_version)
            if error is None:
                cur = c.execute(
                    "UPDATE approvals SET status='CONSUMED', consumed_at=? WHERE id=? AND status='APPROVED' AND nonce=?",
                    (now_iso(), approval_id, r["nonce"]),
                )
                if cur.rowcount != 1:
                    error = ApprovalError(ErrorCode.APPROVAL_INVALID, "approval nonce already consumed")
        if error is not None:
            self._notify(approval_id)
            raise error

    def _check_binding(self, c: Any, r: Any, intent: ActionIntent, *, owner: str, fingerprint: str | None,
                       destination: str | None, current_preconditions: dict[str, Any],
                       current_policy_version: str) -> ApprovalError | None:
        def invalid(code: ErrorCode, why: str) -> ApprovalError:
            c.execute("UPDATE approvals SET status='INVALIDATED', decision_note=? WHERE id=? AND status='APPROVED'",
                      (why, r["id"]))
            return ApprovalError(code, why)

        status = r["status"]
        if status == ApprovalStatus.CONSUMED.value:
            return ApprovalError(ErrorCode.APPROVAL_INVALID, "approval was already used")
        if status == ApprovalStatus.DENIED.value:
            return ApprovalError(ErrorCode.APPROVAL_DENIED, "the owner denied this action")
        if status != ApprovalStatus.APPROVED.value:
            return ApprovalError(ErrorCode.APPROVAL_EXPIRED if status == "EXPIRED" else ErrorCode.APPROVAL_INVALID,
                                 f"approval is {status.lower()}")
        if parse_iso(r["expires_at"]) <= utcnow():  # type: ignore[operator]
            c.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?", (r["id"],))
            return ApprovalError(ErrorCode.APPROVAL_EXPIRED, "approval expired")
        if r["owner"] != owner:
            return invalid(ErrorCode.APPROVAL_INVALID, "approval belongs to a different owner")
        if r["task_id"] != intent.task_id or r["action_id"] != intent.action_id:
            return invalid(ErrorCode.APPROVAL_INVALID, "approval is bound to a different action")
        preconditions = loads(r["preconditions_json"], {})
        if not self._verify_sig(r, preconditions):
            return invalid(ErrorCode.APPROVAL_INVALID, "approval signature does not verify")
        if r["policy_version"] != current_policy_version:
            return invalid(ErrorCode.APPROVAL_INVALID, "policy changed since the approval")
        if (destination or None) != (r["destination"] or None):
            return invalid(ErrorCode.APPROVAL_INVALID, "destination changed since the approval")
        if digest(binding_view(intent, fingerprint, destination)) != r["action_digest"]:
            return invalid(ErrorCode.APPROVAL_INVALID, "action inputs changed since the approval")
        if preconditions != current_preconditions:
            return invalid(ErrorCode.APPROVAL_INVALID, "target state changed since the approval")
        return None

    def _verify_sig(self, r: Any, preconditions: dict[str, Any]) -> bool:
        message = canonical_json({
            "owner": r["owner"], "task": r["task_id"], "digest": r["action_digest"], "destination": r["destination"],
            "preconditions": preconditions, "policy": r["policy_version"], "expires": r["expires_at"], "nonce": r["nonce"],
        })
        return hmac_verify(self.key, message, r["signature"])
