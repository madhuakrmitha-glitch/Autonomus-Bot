"""Channel transport interface and a recording implementation for tests and dry runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Buttons = list[list[tuple[str, str]]]


class Channel(Protocol):
    async def send_text(self, chat_id: str, text: str, buttons: Buttons | None = None,
                        reply_to: str | None = None) -> dict[str, Any]: ...
    async def edit_text(self, chat_id: str, message_id: str, text: str, buttons: Buttons | None = None) -> dict[str, Any]: ...
    async def send_document(self, chat_id: str, filename: str, data: bytes, caption: str | None = None) -> dict[str, Any]: ...
    async def answer_callback(self, callback_id: str, text: str) -> None: ...


@dataclass
class SentMessage:
    kind: str
    chat_id: str
    text: str
    message_id: str
    buttons: Buttons | None = None
    filename: str | None = None
    data: bytes | None = None
    edits: list[str] = field(default_factory=list)


class RecordingChannel:
    """Keeps every outbound message in memory (tests, `omca demo`)."""

    def __init__(self) -> None:
        self.messages: list[SentMessage] = []
        self.callback_answers: list[tuple[str, str]] = []
        self._next = 1000
        self.fail_next = 0

    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    async def send_text(self, chat_id, text, buttons=None, reply_to=None):
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("simulated channel failure")
        m = SentMessage("text", chat_id, text, self._id(), buttons)
        self.messages.append(m)
        return {"chat_id": chat_id, "message_id": m.message_id}

    async def edit_text(self, chat_id, message_id, text, buttons=None):
        for m in self.messages:
            if m.message_id == message_id:
                m.edits.append(text)
                m.text = text
                m.buttons = buttons
        return {"chat_id": chat_id, "message_id": message_id}

    async def send_document(self, chat_id, filename, data, caption=None):
        m = SentMessage("document", chat_id, caption or "", self._id(), filename=filename, data=data)
        self.messages.append(m)
        return {"chat_id": chat_id, "message_id": m.message_id}

    async def answer_callback(self, callback_id, text):
        self.callback_answers.append((callback_id, text))

    def texts(self) -> list[str]:
        return [m.text for m in self.messages]

    def documents(self) -> list[SentMessage]:
        return [m for m in self.messages if m.kind == "document"]
