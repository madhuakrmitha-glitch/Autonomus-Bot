"""Secret references.

Configuration never holds literal secrets. A secret is referenced as one of:

* ``env:NAME``                  - environment variable
* ``keyring:SERVICE/USERNAME``  - OS credential store (Windows Credential Manager, macOS Keychain)
* ``file:PATH``                 - file contents (for container/VM secret mounts), stripped
* ``dotenv:PATH#NAME``          - one ``NAME=value`` line of a dotenv file, e.g. the
  ``~/.openclaw/.env`` that OpenClaw also reads (useful for launchd agents, which do not
  inherit shell environment variables)

``resolve`` returns ``None`` when the reference is unset so callers can keep a
route disabled instead of silently activating it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_REF = re.compile(r"^(env|keyring|file|dotenv):(.+)$")


class SecretError(ValueError):
    pass


def is_secret_ref(value: str | None) -> bool:
    return bool(value) and bool(_REF.match(value or ""))


def validate_ref(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not _REF.match(value):
        raise SecretError(
            f"{field} must be a secret reference (env:NAME, keyring:SERVICE/USER, file:PATH or dotenv:PATH#NAME), "
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
    if scheme == "dotenv":
        if "#" not in rest:
            raise SecretError("dotenv reference must be dotenv:PATH#NAME")
        path_s, name = rest.rsplit("#", 1)
        p = Path(os.path.expandvars(path_s)).expanduser()
        if not p.exists():
            return None
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().removeprefix("export ").strip() == name:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value or None
        return None
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
