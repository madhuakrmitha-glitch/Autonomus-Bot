"""Network destination checks shared by the policy gate, the coordinator's fetchers and the worker.

The policy classifier checks the URL text. Fetchers also resolve the host and check every redirect
hop, so a public name that resolves or redirects to a private address is refused unless the owner
allowed that host (``search.allowed_private_hosts``) or origin (an attachment's
``allowed_private_origins``).

Residual risk: a name is resolved for the check and again by the HTTP client, so a DNS answer
that changes between the two (rebinding) is not caught. See docs/limitations.md.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_NUMERIC_HOST = re.compile(r"[0-9a-fx.]+", re.I)


class BlockedDestination(Exception):
    pass


def origin_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def origin_matches(origin: str | None, pattern: str) -> bool:
    if not origin:
        return False
    regex = "^" + re.escape(pattern.lower()).replace(r"\*", "[^/]*") + "$"
    return re.match(regex, origin.lower()) is not None


def normalize_host(host: str | None) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP literal, including the legacy IPv4 forms browsers accept (``127.1``, ``0x7f.1``, ``2130706433``)."""
    h = normalize_host(host)
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if _NUMERIC_HOST.fullmatch(h):
        try:
            return ipaddress.ip_address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return not ip.is_global or ip.is_multicast or ip.is_unspecified


def is_private_host(host: str) -> bool:
    h = normalize_host(host)
    if not h or h == "localhost" or h.endswith(LOCAL_SUFFIXES):
        return True
    ip = parse_ip(h)
    if ip is not None:
        return ip_is_private(ip)
    return "." not in h  # single-label names resolve on the local network


def url_problem(url: str) -> str | None:
    """Reasons a URL is refused before any network access (shape only)."""
    if any(ch in url for ch in "\\ ") or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return "URL contains characters that browsers and HTTP clients interpret differently"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "URL is malformed"
    if parts.scheme not in {"http", "https"}:
        return f"scheme {parts.scheme or '(none)'} is not allowed"
    if "@" in parts.netloc:
        return "URLs with embedded credentials are not allowed"
    if not normalize_host(parts.hostname):
        return "URL has no host"
    return None


async def resolve_private(host: str, timeout: float = 5.0) -> list[str]:
    """Private addresses ``host`` resolves to (empty when it does not resolve: the fetch then fails on its own)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(resolve_private_sync, host), timeout)
    except TimeoutError:
        return []


def resolve_private_sync(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(normalize_host(host), None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    out: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        if ip_is_private(ip):
            out.append(str(ip))
    return out


class UrlGuard:
    """Decides whether this process may fetch a URL (no credentials, no private network unless allowed)."""

    def __init__(self, *, allowed_hosts: Iterable[str] = (), allowed_origins: Iterable[str] = (),
                 block_domains: Iterable[str] = (), resolve: bool = True):
        self.allowed_hosts = {normalize_host(h) for h in allowed_hosts}
        self.allowed_origins = list(allowed_origins)
        self.block_domains = [normalize_host(d) for d in block_domains]
        self.resolve = resolve

    def private_allowed(self, url: str) -> bool:
        host = normalize_host(urlsplit(url).hostname)
        return host in self.allowed_hosts or any(origin_matches(origin_of(url), p) for p in self.allowed_origins)

    def static_problem(self, url: str) -> str | None:
        problem = url_problem(url)
        if problem:
            return problem
        host = normalize_host(urlsplit(url).hostname)
        if any(host == d or host.endswith("." + d) for d in self.block_domains):
            return f"domain {host} is blocked"
        if is_private_host(host) and not self.private_allowed(url):
            return f"private network host {host} is not an allowed origin"
        return None

    async def check(self, url: str) -> None:
        problem = self.static_problem(url)
        if problem:
            raise BlockedDestination(problem)
        host = normalize_host(urlsplit(url).hostname)
        if self.resolve and parse_ip(host) is None and not self.private_allowed(url):
            bad = await resolve_private(host)
            if bad:
                raise BlockedDestination(f"{host} resolves to the private address {bad[0]}")

    def check_sync(self, url: str) -> None:
        """Blocking variant for code running in a worker thread."""
        problem = self.static_problem(url)
        if problem:
            raise BlockedDestination(problem)
        host = normalize_host(urlsplit(url).hostname)
        if self.resolve and parse_ip(host) is None and not self.private_allowed(url):
            bad = resolve_private_sync(host)
            if bad:
                raise BlockedDestination(f"{host} resolves to the private address {bad[0]}")
