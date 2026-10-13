"""Model call value types (OpenAI chat-completions message format is the internal lingua franca)."""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageInput:
    data: bytes
    media_type: str = "image/png"
    artifact_id: str | None = None
    label: str | None = None

    def size(self) -> tuple[int, int]:
        from PIL import Image

        with Image.open(io.BytesIO(self.data)) as im:
            return im.size


@dataclass
class ImageTransform:
    """How an image was resized before it reached the model (for coordinate mapping)."""

    artifact_id: str | None
    original_width: int
    original_height: int
    sent_width: int
    sent_height: int

    @property
    def scale(self) -> float:
        return self.sent_width / self.original_width if self.original_width else 1.0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str
    parse_error: str | None = None


@dataclass
class ModelResponse:
    route_id: str
    model: str
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: dict[str, Any] | None
    call_id: str
    latency_ms: int
    image_transforms: list[ImageTransform] = field(default_factory=list)
    reasoning: str | None = None
    raw: dict[str, Any] | None = None
    # provider reasoning blocks (e.g. signed thinking); sent back with the tool results of a multi-step exchange
    reasoning_details: list[dict[str, Any]] | None = None

    def json(self) -> Any:
        """Parse a JSON answer from text (tolerates code fences)."""
        return extract_json(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "model": self.model,
            "text": self.text,
            "tool_calls": [tc.__dict__ for tc in self.tool_calls],
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "call_id": self.call_id,
            "latency_ms": self.latency_ms,
            "image_transforms": [t.__dict__ for t in self.image_transforms],
            "reasoning_details": self.reasoning_details,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ModelResponse:
        return ModelResponse(
            route_id=d["route_id"], model=d["model"], text=d["text"],
            tool_calls=[ToolCall(**tc) for tc in d["tool_calls"]], finish_reason=d.get("finish_reason"),
            usage=d.get("usage"), call_id=d["call_id"], latency_ms=d.get("latency_ms", 0),
            image_transforms=[ImageTransform(**t) for t in d.get("image_transforms", [])],
            reasoning_details=d.get("reasoning_details"),
        )


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def thinking_of(text: str) -> str:
    """The contents of any <think> blocks in a model answer (what the model reasoned before replying)."""
    return "\n\n".join(m.strip() for m in re.findall(r"<think>(.*?)</think>", text or "", re.S) if m.strip())


def extract_json(text: str) -> Any:
    text = strip_thinking(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("model answer did not contain valid JSON")


def image_part(img: ImageInput) -> dict[str, Any]:
    b64 = base64.b64encode(img.data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{img.media_type};base64,{b64}"}}


def text_of(messages: list[dict[str, Any]]) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        for tc in m.get("tool_calls") or []:
            out.append(json.dumps(tc))
    return "\n".join(out)


def count_images(messages: list[dict[str, Any]]) -> list[str]:
    urls = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            urls.extend(p["image_url"]["url"] for p in c if isinstance(p, dict) and p.get("type") == "image_url")
    return urls
