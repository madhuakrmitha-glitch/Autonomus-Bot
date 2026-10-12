"""Durable owner notifications (outbox).

Messages are written to the outbox first and delivered by a sender loop with retries, so
status, approvals and results survive restarts. Deterministic keys deduplicate messages
that a replayed workflow step would otherwise send twice. All messages go to the owner's
DM; nothing here can address another person.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from ..config import ApplicationConfig
from ..contracts import ApprovalRequest
from ..db import Database, dumps, loads
from ..util import digest, display_time, new_id, now_iso
from .base import Buttons, Channel


class Notifier:
    def __init__(self, db: Database, config: ApplicationConfig, channel: Channel | None):
        self.db = db
        self.config = config
        self.channel = channel
        self._wake = asyncio.Event()

    def owner_chat(self) -> str | None:
        ids = self.owner_ids()
        return str(ids[0]) if ids else None

    def owner_ids(self) -> list[int]:
        ids = list(self.config.owner.telegram_user_ids)
        paired = self.db.kv_get("paired_owner_id")
        if paired and int(paired) not in ids:
            ids.append(int(paired))
        return ids

    # -- enqueue ------------------------------------------------------------------------------------
    def _enqueue(self, kind: str, payload: dict[str, Any], task_id: str | None, key: str | None) -> str:
        oid = f"out_{digest(key)[:24]}" if key else new_id("out")
        self.db.execute(
            "INSERT OR IGNORE INTO outbox(id, task_id, kind, payload_json, status, created_at) VALUES (?,?,?,?,'PENDING',?)",
            (oid, task_id, kind, dumps(payload), now_iso()),
        )
        self._wake.set()
        return oid

    def text(self, text: str, *, task_id: str | None = None, buttons: Buttons | None = None, key: str | None = None,
             reply_to: str | None = None) -> str:
        return self._enqueue("text", {"text": text, "buttons": buttons, "reply_to": reply_to}, task_id, key)

    def edit(self, message_ref: str, text: str, *, task_id: str | None = None, buttons: Buttons | None = None,
             key: str | None = None) -> str:
        return self._enqueue("edit", {"ref": message_ref, "text": text, "buttons": buttons}, task_id, key)

    def document(self, filename: str, data: bytes, caption: str, *, task_id: str | None = None, key: str | None = None) -> str:
        return self._enqueue("document", {"filename": filename, "data_b64": base64.b64encode(data).decode("ascii"),
                                          "caption": caption}, task_id, key)

    def approval(self, ap: ApprovalRequest, task_short: str, *, approve_cmd: str = "/approve", deny_cmd: str = "/deny") -> str:
        tz = self.config.owner.display_timezone
        text = (
            f"Approval needed - task {task_short}\n\n"
            f"Action: {ap.preview}\n"
            f"Operation: {ap.operation}\n"
            f"Destination: {ap.destination or '-'}\n"
            f"Consequence: {ap.consequence}\n"
            f"Expires: {display_time(ap.expires_at, tz)}\n\n"
            f"Approve once or deny. Reply {approve_cmd} {ap.approval_id} or {deny_cmd} {ap.approval_id} if buttons "
            "are unavailable."
        )
        buttons = [[("Approve once", f"omca:apr:{ap.approval_id}:y"), ("Deny", f"omca:apr:{ap.approval_id}:n")]]
        return self.text(text, task_id=ap.task_id, buttons=buttons, key=f"approval:{ap.approval_id}")

    # -- delivery -------------------------------------------------------------------------------------
    def result(self, outbox_id: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT status, result_json FROM outbox WHERE id=?", (outbox_id,))
        if r is None or r["status"] != "SENT":
            return None
        return loads(r["result_json"], {})

    async def flush(self, limit: int = 50) -> int:
        if self.channel is None:
            return 0
        chat = self.owner_chat()
        if chat is None:
            return 0
        rows = self.db.query("SELECT * FROM outbox WHERE status='PENDING' ORDER BY created_at LIMIT ?", (limit,))
        sent = 0
        for r in rows:
            payload = loads(r["payload_json"], {})
            try:
                if r["kind"] == "text":
                    res = await self.channel.send_text(chat, payload["text"], _buttons(payload.get("buttons")),
                                                       payload.get("reply_to"))
                elif r["kind"] == "edit":
                    target = self.result(payload["ref"]) if payload["ref"].startswith("out_") else {"message_id": payload["ref"]}
                    if not target:
                        continue  # original not sent yet; retry later
                    res = await self.channel.edit_text(chat, target["message_id"], payload["text"],
                                                       _buttons(payload.get("buttons")))
                elif r["kind"] == "document":
                    res = await self.channel.send_document(chat, payload["filename"], base64.b64decode(payload["data_b64"]),
                                                           payload.get("caption"))
                else:
                    raise ValueError(f"unknown outbox kind {r['kind']}")
            except Exception as exc:  # noqa: BLE001 - keep for retry, give up after repeated failures
                attempts = r["attempts"] + 1
                status = "FAILED" if attempts >= 8 or getattr(exc, "permanent", False) else "PENDING"
                self.db.execute("UPDATE outbox SET attempts=?, last_error=?, status=? WHERE id=?",
                                (attempts, f"{type(exc).__name__}: {exc}"[:500], status, r["id"]))
                continue
            self.db.execute("UPDATE outbox SET status='SENT', result_json=?, sent_at=?, attempts=attempts+1 WHERE id=?",
                            (dumps(res), now_iso(), r["id"]))
            sent += 1
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.flush()
            except Exception:  # noqa: BLE001 - never let the sender loop die
                pass
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except TimeoutError:
                pass

    def pending(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM outbox WHERE status='PENDING' ORDER BY created_at")]


def _buttons(raw: Any) -> Buttons | None:
    if not raw:
        return None
    return [[(b[0], b[1]) for b in row] for row in raw]
