"""Local web UI inbox: the owner's conversation as shown in the browser.

The notifier mirrors every outbox message here before (and independently of) Telegram delivery,
so the web UI and Telegram can be used side by side. Messages typed in the UI are recorded as
owner rows. Every insert or edit takes a new ``rev`` so the UI polls for changes since the last
revision it saw.
"""

from __future__ import annotations

import base64
from typing import Any

from ..db import Database, dumps, loads
from ..util import now_iso

# Task events worth showing as live activity (the rest, such as stale receipts, are audit detail).
ACTIVITY_KINDS = ("progress", "model_start", "model_done", "model_error", "action_intent", "policy", "action_receipt",
                  "limitation", "reconciled", "status", "owner_steer", "owner_owner_input")
ACTIVITY_LIMIT = 600


class WebInbox:
    def __init__(self, db: Database):
        self.db = db

    def _next_rev(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(rev), 0) + 1 FROM web_messages"))

    def add(self, role: str, text: str, *, kind: str = "text", task_id: str | None = None,
            buttons: Any = None, filename: str | None = None, outbox_id: str | None = None,
            conversation_id: str | None = None) -> int:
        with self.db.tx():
            if outbox_id is not None:
                existing = self.db.scalar("SELECT id FROM web_messages WHERE outbox_id=?", (outbox_id,))
                if existing is not None:
                    return int(existing)
            now = now_iso()
            if conversation_id is None and task_id is not None:
                conversation_id = self.db.scalar("SELECT conversation_id FROM tasks WHERE id=?", (task_id,))
            cur = self.db.execute(
                "INSERT INTO web_messages(outbox_id, task_id, conversation_id, role, kind, text, buttons_json, filename, "
                "rev, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (outbox_id, task_id, conversation_id, role, kind, text, dumps(buttons) if buttons else None, filename,
                 self._next_rev(), now, now),
            )
            return int(cur.lastrowid)

    def link_task(self, message_id: int, task_id: str) -> None:
        """Thread an owner message under the task it created or addressed."""
        self.db.execute("UPDATE web_messages SET task_id=?, "
                        "conversation_id=COALESCE(conversation_id, (SELECT conversation_id FROM tasks WHERE id=?)), "
                        "rev=?, updated_at=? WHERE id=? AND task_id IS NULL",
                        (task_id, task_id, self._next_rev(), now_iso(), message_id))

    def _target(self, ref: str) -> int | None:
        """Web message an edit refers to: an outbox id, or a channel message id recorded in an outbox result."""
        if ref.startswith("out_"):
            mid = self.db.scalar("SELECT id FROM web_messages WHERE outbox_id=?", (ref,))
            return int(mid) if mid is not None else None
        for r in self.db.query("SELECT id, result_json FROM outbox WHERE status='SENT' AND kind != 'edit' "
                               "ORDER BY sent_at DESC LIMIT 500"):
            if str((loads(r["result_json"], {}) or {}).get("message_id")) == str(ref):
                return self._target(r["id"])
        return None

    def mirror(self, row: Any) -> int | None:
        """Apply one outbox row (once); returns the web message id it created or edited."""
        with self.db.tx():
            if self.db.execute("INSERT OR IGNORE INTO web_mirrored(outbox_id) VALUES (?)", (row["id"],)).rowcount == 0:
                return self._target(row["id"]) if row["kind"] != "edit" else None
            payload = loads(row["payload_json"], {})
            if row["kind"] == "text":
                return self.add("assistant", payload["text"], task_id=row["task_id"], buttons=payload.get("buttons"),
                                outbox_id=row["id"])
            if row["kind"] == "document":
                return self.add("assistant", payload.get("caption") or "", kind="document", task_id=row["task_id"],
                                filename=payload["filename"], outbox_id=row["id"])
            if row["kind"] == "edit":
                mid = self._target(payload["ref"])
                if mid is not None:
                    self.db.execute("UPDATE web_messages SET text=?, buttons_json=?, rev=?, updated_at=? WHERE id=?",
                                    (payload["text"], dumps(payload["buttons"]) if payload.get("buttons") else None,
                                     self._next_rev(), now_iso(), mid))
                return mid
            return None

    def feed(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM web_messages WHERE rev > ? ORDER BY rev LIMIT ?", (after, limit))
        return [{
            "id": r["id"], "rev": r["rev"], "role": r["role"], "kind": r["kind"], "text": r["text"],
            "task_id": r["task_id"], "conversation_id": r["conversation_id"], "buttons": loads(r["buttons_json"], None), "filename": r["filename"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        } for r in rows]

    def document(self, message_id: int) -> tuple[str, bytes] | None:
        r = self.db.one("SELECT o.payload_json FROM web_messages w JOIN outbox o ON o.id = w.outbox_id "
                        "WHERE w.id=? AND w.kind='document'", (message_id,))
        if r is None:
            return None
        payload = loads(r["payload_json"], {})
        return payload["filename"], base64.b64decode(payload["data_b64"])

    # -- live activity -----------------------------------------------------------------------------
    def last_event_id(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(id), 0) FROM task_events"))

    def activity(self, after: int = 0, *, task_id: str | None = None, conversation: str | None = None,
                 limit: int = ACTIVITY_LIMIT) -> list[dict[str, Any]]:
        """Task events in id order. With a task or chat filter and no cursor, the most recent ``limit`` events."""
        marks = ",".join("?" * len(ACTIVITY_KINDS))
        where, params = [f"e.kind IN ({marks})", "e.task_id IS NOT NULL", "e.id > ?"], [*ACTIVITY_KINDS, after]
        if task_id:
            where.append("e.task_id = ?")
            params.append(task_id)
        if conversation:
            where.append("(t.conversation_id = ? OR t.id = ?)")
            params += [conversation, conversation]
        sql = ("SELECT e.id, e.task_id, t.conversation_id, e.ts, e.kind, e.data_json FROM task_events e "
               "JOIN tasks t ON t.id = e.task_id WHERE " + " AND ".join(where))
        if after <= 0 and (task_id or conversation):
            rows = list(reversed(self.db.query(sql + " ORDER BY e.id DESC LIMIT ?", (*params, limit))))
        else:
            rows = self.db.query(sql + " ORDER BY e.id LIMIT ?", (*params, limit))
        return [{"id": r["id"], "task_id": r["task_id"], "conversation_id": r["conversation_id"], "ts": r["ts"],
                 "kind": r["kind"], "data": loads(r["data_json"], {})} for r in rows]
