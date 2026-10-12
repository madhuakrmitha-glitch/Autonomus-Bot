"""Secret references.

Configuration never holds literal secrets. A secret is referenced as one of:

* ``env:NAME``                  - environment variable
* ``keyring:SERVICE/USERNAME``  - OS credential store (Windows Credential Manager on Windows)
* ``file:PATH``                 - file contents (for container/VM secret mounts), stripped

``resolve`` returns ``None`` when the reference is unset so callers can keep a
route disabled instead of silently activating it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_REF = re.compile(r"^(env|keyring|file):(.+)$")


class SecretError(ValueError):
    pass


def is_secret_ref(value: str | None) -> bool:
    return bool(value) and bool(_REF.match(value or ""))


def validate_ref(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not _REF.match(value):
        raise SecretError(
            f"{field} must be a secret reference (env:NAME, keyring:SERVICE/USER or file:PATH), "
            "not a literal value"
        )
    return value


def resolve(ref: str | None) -> str | None:
    if not ref:
        return None
    m = _REF.match(ref)
    if not m:
        raise SecretError(f"not a secret reference: {ref[:12]}...")
    scheme, rest = m.group(1), m.group(2)
    if scheme == "env":
        value = os.environ.get(rest)
        return value or None
    if scheme == "file":
        p = Path(rest).expanduser()
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8").strip() or None
    if scheme == "keyring":
        if "/" not in rest:
            raise SecretError("keyring reference must be keyring:SERVICE/USERNAME")
        service, username = rest.split("/", 1)
        try:
            import keyring  # imported lazily: optional backends differ per OS
        except ImportError as exc:  # pragma: no cover
            raise SecretError("keyring package not installed") from exc
        try:
            return keyring.get_password(service, username)
        except Exception:  # noqa: BLE001 - backend unavailable is "unset", not a crash
            return None
    raise SecretError(f"unsupported secret scheme {scheme}")


def require(ref: str | None, what: str) -> str:
    value = resolve(ref)
    if not value:
        raise SecretError(f"secret for {what} is not available ({ref or 'no reference configured'})")
    return value


def store_keyring(ref: str, value: str) -> None:
    m = _REF.match(ref)
    if not m or m.group(1) != "keyring":
        raise SecretError("only keyring: references can be written by the CLI")
    service, username = m.group(2).split("/", 1)
    import keyring

    keyring.set_password(service, username, value)
