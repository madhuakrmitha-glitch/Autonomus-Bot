"""Small shared helpers: identifiers, UTC time, hashing and canonical JSON."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    """Time-sortable identifier: ``<prefix>_<26 char ULID-style body>``."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime; store machine timestamps in UTC")
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def in_seconds(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))  # type: ignore[return-value]


def display_time(dt: datetime | str | None, tz: str) -> str:
    if dt is None:
        return "-"
    if isinstance(dt, str):
        dt = parse_iso(dt)
    assert dt is not None
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for digests and idempotency keys."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(value: Any) -> str:
    return sha256_text(canonical_json(value))


def hmac_sign(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def hmac_verify(key: bytes, message: str, signature: str) -> bool:
    return hmac.compare_digest(hmac_sign(key, message), signature)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
