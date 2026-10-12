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


class WebInbox:
    def __init__(self, db: Database):
        self.db = db

    def _next_rev(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(rev), 0) + 1 FROM web_messages"))

    def add(self, role: str, text: str, *, kind: str = "text", task_id: str | None = None,
            buttons: Any = None, filename: str | None = None, outbox_id: str | None = None) -> int:
        with self.db.tx():
            if outbox_id is not None:
                existing = self.db.scalar("SELECT id FROM web_messages WHERE outbox_id=?", (outbox_id,))
                if existing is not None:
                    return int(existing)
            now = now_iso()
            cur = self.db.execute(
                "INSERT INTO web_messages(outbox_id, task_id, role, kind, text, buttons_json, filename, rev, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (outbox_id, task_id, role, kind, text, dumps(buttons) if buttons else None, filename, self._next_rev(),
                 now, now),
            )
            return int(cur.lastrowid)

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
            "task_id": r["task_id"], "buttons": loads(r["buttons_json"], None), "filename": r["filename"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        } for r in rows]

    def document(self, message_id: int) -> tuple[str, bytes] | None:
        r = self.db.one("SELECT o.payload_json FROM web_messages w JOIN outbox o ON o.id = w.outbox_id "
                        "WHERE w.id=? AND w.kind='document'", (message_id,))
        if r is None:
            return None
        payload = loads(r["payload_json"], {})
        return payload["filename"], base64.b64decode(payload["data_b64"])
