"""Browser logins the model can use without ever seeing them.

The model types a placeholder such as ``{{cred:github.password}}``. The policy gate only checks that the
credential exists and that the page is on the credential's origin; the worker (which holds the secret
references) substitutes the real value just before typing, so passwords never reach prompts, the action
journal or approval records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import BrowserCredential
from .netguard import origin_of
from .secrets import resolve

PLACEHOLDER = re.compile(r"^\{\{cred:([A-Za-z0-9_-]{1,40})\.(username|password)\}\}$")


def parse_placeholder(text: str) -> tuple[str, str] | None:
    m = PLACEHOLDER.fullmatch(text.strip())
    return (m.group(1), m.group(2)) if m else None


def credential_for(creds: Iterable[BrowserCredential], cred_id: str) -> BrowserCredential | None:
    return next((c for c in creds if c.id == cred_id), None)


def placeholder_problem(text: str, creds: Iterable[BrowserCredential], page_url: str | None) -> str | None:
    """None when ``text`` is a valid placeholder usable on this page; otherwise why it is refused."""
    parsed = parse_placeholder(text)
    if parsed is None:
        return "not a credential placeholder"
    cred = credential_for(creds, parsed[0])
    if cred is None:
        return f"unknown credential '{parsed[0]}'"
    if origin_of(page_url) != cred.origin:
        return f"credential '{cred.id}' is only valid on {cred.origin}"
    return None


def substitute(text: str, creds: Iterable[BrowserCredential], page_url: str | None) -> str:
    """Worker side: the secret for a placeholder (checked against the live page origin), else ``text`` unchanged."""
    parsed = parse_placeholder(text)
    if parsed is None:
        return text
    problem = placeholder_problem(text, creds, page_url)
    if problem:
        raise PermissionError(problem)
    cred = credential_for(creds, parsed[0])
    assert cred is not None
    value = resolve(cred.username_ref if parsed[1] == "username" else cred.password_ref)
    if not value:
        raise PermissionError(f"credential '{cred.id}' has no {parsed[1]} available")
    return value


def describe(creds: Iterable[BrowserCredential]) -> str:
    """Prompt text listing what is available, never the values."""
    lines = [f"- {c.id} on {c.origin}: {{{{cred:{c.id}.username}}}} and {{{{cred:{c.id}.password}}}}" for c in creds]
    return "\n".join(lines)
