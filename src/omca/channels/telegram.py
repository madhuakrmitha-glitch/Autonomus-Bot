"""Telegram Bot API transport.

Used for outbound delivery in both modes. In ``direct`` mode the coordinator also long-polls
``getUpdates``; in ``openclaw`` mode OpenClaw owns polling and forwards owner messages and
``omca:`` button callbacks through the plugin bridge. Sending with the same bot token does
not conflict with OpenClaw's polling.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import Buttons

MAX_TEXT = 4000


def keyboard(buttons: Buttons | None) -> dict[str, Any] | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": label, "callback_data": data[:64]} for label, data in row] for row in buttons]}


class TelegramApiError(Exception):
    def __init__(self, message: str, retry_after: float | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramBotApi:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org", client: httpx.AsyncClient | None = None):
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=60)

    async def call(self, method: str, payload: dict[str, Any] | None = None, *, files: dict[str, Any] | None = None,
                   timeout: float = 60) -> Any:
        url = f"{self._base}/{method}"
        try:
            if files:
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                        for k, v in (payload or {}).items() if v is not None}
                resp = await self._client.post(url, data=data, files=files, timeout=timeout)
            else:
                resp = await self._client.post(url, json={k: v for k, v in (payload or {}).items() if v is not None},
                                               timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramApiError(f"telegram transport error: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise TelegramApiError(f"telegram returned HTTP {resp.status_code}") from exc
        if not body.get("ok"):
            params = body.get("parameters") or {}
            raise TelegramApiError(f"telegram {method} failed: {body.get('description')}",
                                   retry_after=params.get("retry_after"),
                                   permanent=resp.status_code in (400, 403) and "retry" not in str(body).lower())
        return body["result"]

    # -- Channel protocol -------------------------------------------------------------------------
    async def send_text(self, chat_id: str, text: str, buttons: Buttons | None = None,
                        reply_to: str | None = None) -> dict[str, Any]:
        res = await self.call("sendMessage", {
            "chat_id": chat_id, "text": text[:MAX_TEXT], "reply_markup": keyboard(buttons),
            "reply_parameters": {"message_id": int(reply_to), "allow_sending_without_reply": True} if reply_to else None,
            "link_preview_options": {"is_disabled": True},
        })
        return {"chat_id": str(res["chat"]["id"]), "message_id": str(res["message_id"])}

    async def edit_text(self, chat_id: str, message_id: str, text: str, buttons: Buttons | None = None) -> dict[str, Any]:
        try:
            await self.call("editMessageText", {"chat_id": chat_id, "message_id": int(message_id), "text": text[:MAX_TEXT],
                                                "reply_markup": keyboard(buttons) or {"inline_keyboard": []}})
        except TelegramApiError as exc:
            if "message is not modified" not in str(exc):
                raise
        return {"chat_id": chat_id, "message_id": message_id}

    async def send_document(self, chat_id: str, filename: str, data: bytes, caption: str | None = None) -> dict[str, Any]:
        res = await self.call("sendDocument", {"chat_id": chat_id, "caption": (caption or "")[:1000]},
                              files={"document": (filename, data)}, timeout=300)
        return {"chat_id": str(res["chat"]["id"]), "message_id": str(res["message_id"]),
                "file_id": (res.get("document") or {}).get("file_id")}

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    # -- inbound (direct mode) -----------------------------------------------------------------------
    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        return await self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                              "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 15)

    async def get_file(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> tuple[str, bytes]:
        info = await self.call("getFile", {"file_id": file_id})
        path = info["file_path"]
        if info.get("file_size") and info["file_size"] > max_bytes:
            raise TelegramApiError("attachment is too large")
        url = self._base.replace("/bot", "/file/bot", 1) + f"/{path}"
        resp = await self._client.get(url, timeout=120)
        resp.raise_for_status()
        return path.rsplit("/", 1)[-1], resp.content


class TelegramPoller:
    """Direct mode: long-poll ``getUpdates`` and hand owner traffic to the owner interface.

    Only private chats are accepted; groups and channels are ignored. The update offset is
    persisted so a restart neither replays nor drops updates.
    """

    def __init__(self, api: TelegramBotApi, iface: Any, db: Any, poll_timeout: int = 30, max_attachment_mb: int = 20):
        self.api = api
        self.iface = iface
        self.db = db
        self.poll_timeout = poll_timeout
        self.max_bytes = max_attachment_mb * 1024 * 1024

    async def run(self, stop: Any) -> None:
        import asyncio

        backoff = 1.0
        while not stop.is_set():
            try:
                await self.poll_once()
                backoff = 1.0
            except TelegramApiError as exc:
                await asyncio.sleep(exc.retry_after or backoff)
                backoff = min(60.0, backoff * 2)
            except Exception:  # noqa: BLE001 - keep polling
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def poll_once(self) -> int:
        from ..policy import Identity

        offset = self.db.kv_get("telegram_offset")
        updates = await self.api.get_updates(offset, self.poll_timeout)
        for u in updates:
            self.db.kv_set("telegram_offset", int(u["update_id"]) + 1)
            if "callback_query" in u:
                cq = u["callback_query"]
                ident = Identity("telegram", str(cq["from"]["id"]))
                reply = await self.iface.handle_callback(ident, cq.get("data", ""), callback_id=str(cq["id"]))
                try:
                    await self.api.answer_callback(cq["id"], reply or "")
                except TelegramApiError:
                    pass
                continue
            msg = u.get("message")
            if not msg or (msg.get("chat") or {}).get("type") != "private":
                continue
            ident = Identity("telegram", str(msg["from"]["id"]))
            attachments = []
            if self.iface.c.is_owner(ident):
                for f in _files(msg):
                    try:
                        name, data = await self.api.get_file(f["file_id"], self.max_bytes)
                        attachments.append((f.get("file_name") or name, data))
                    except Exception:  # noqa: BLE001
                        continue
            reply_to = (msg.get("reply_to_message") or {}).get("message_id")
            reply = await self.iface.handle_message(ident, msg.get("text") or msg.get("caption") or "",
                                                    update_id=str(u["update_id"]),
                                                    reply_to_message_id=str(reply_to) if reply_to else None,
                                                    attachments=attachments)
            if reply:
                await self.api.send_text(str(msg["chat"]["id"]), reply)
        return len(updates)


def _files(msg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    if msg.get("document"):
        out.append(msg["document"])
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        out.append({**best, "file_name": f"photo_{best.get('file_unique_id', 'image')}.jpg"})
    return out
