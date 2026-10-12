"""Provider-independent search and retrieval (FR03, spec section 10).

Task logic only sees the normalised ``SearchResult`` and ``RetrievedPage`` contracts, so a
provider can be replaced without changing research logic. Adapters never use supplier AI
features (summaries, LLM extraction): planning, summarisation and visual analysis stay on
approved open models.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from ..config import ApplicationConfig
from ..contracts import RetrievedPage, SearchResult
from ..secrets import resolve
from ..util import now_iso, sha256_text

ALLOWED_FIRECRAWL_FORMATS = {"markdown", "html", "rawHtml", "links", "screenshot"}
TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$)")


class SearchError(Exception):
    pass


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and not ((scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)) else ""
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)])
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, host + port, path, query, ""))


# ---------------------------------------------------------------------------
# HTML extraction (stdlib only)
# ---------------------------------------------------------------------------

BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table",
              "br", "header", "footer", "blockquote", "pre", "figcaption", "dd", "dt"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "iframe"}
DATE_META = {"article:published_time", "og:published_time", "datepublished", "date", "dc.date", "pubdate",
             "publish-date", "sailthru.date", "article:modified_time"}


class _Extractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self.date: str | None = None
        self.images: list[str] = []
        self.ld_json: list[str] = []
        self._in_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            if tag == "script" and a.get("type") == "application/ld+json":
                self._in_ld = True
            self.skip += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "meta":
            name = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if name in DATE_META and a.get("content") and not self.date:
                self.date = a["content"]
        if tag == "time" and a.get("datetime") and not self.date:
            self.date = a["datetime"]
        if tag == "img" and a.get("src") and len(self.images) < 50:
            self.images.append(urljoin(self.base, a["src"]))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            self._in_ld = False
            return
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self.ld_json.append(data)
            return
        if self.skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
        out, blank = [], 0
        for ln in lines:
            if ln:
                out.append(ln)
                blank = 0
            elif not blank:
                out.append("")
                blank = 1
        return "\n".join(out).strip()

    def published(self) -> str | None:
        if self.date:
            return self.date
        for blob in self.ld_json:
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', blob)
            if m:
                return m.group(1)
        return None


def extract_html(html: str, base_url: str) -> dict[str, Any]:
    p = _Extractor(base_url)
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup still yields partial text
        pass
    return {"title": re.sub(r"\s+", " ", p.title).strip(), "text": p.text(), "source_date": p.published(),
            "images": p.images}


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class SearchAdapter(Protocol):
    name: str

    async def search(self, query: str, limit: int) -> list[SearchResult]: ...


class RetrievalAdapter(Protocol):
    name: str

    async def retrieve(self, url: str, formats: list[str]) -> RetrievedPage: ...


class FirecrawlAdapter:
    """Firecrawl REST API (v2 by default). Only plain retrieval formats are used."""

    name = "firecrawl"

    def __init__(self, base_url: str, api_key: str | None, api_version: str = "v2", timeout: float = 60,
                 client: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.version = api_version
        self.timeout = timeout
        self.client = client

    def _headers(self) -> dict[str, str]:
        if not self.key:
            raise SearchError("Firecrawl API key is not configured (search.firecrawl.api_key_ref)")
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.post(f"{self.base}/{self.version}/{path}", json=body, headers=self._headers(),
                                     timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SearchError(f"firecrawl {path} transport error: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code >= 400:
            raise SearchError(f"firecrawl {path} HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not data.get("success", True):
            raise SearchError(f"firecrawl {path} failed: {data.get('error')}")
        return data

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        body: dict[str, Any] = {"query": query, "limit": limit}
        if self.version == "v2":
            body["sources"] = ["web"]
        data = await self._post("search", body)
        raw = data.get("data")
        items = raw.get("web", []) if isinstance(raw, dict) else (raw or [])
        now = now_iso()
        out = []
        for i, it in enumerate(items):
            url = it.get("url") or (it.get("metadata") or {}).get("sourceURL")
            if not url:
                continue
            out.append(SearchResult(provider=self.name, rank=i + 1, title=it.get("title") or url,
                                    url=canonical_url(url), snippet=it.get("description") or it.get("snippet") or "",
                                    retrieved_at=now, source_date=it.get("date") or it.get("publishedDate")))
        return out

    async def retrieve(self, url: str, formats: list[str]) -> RetrievedPage:
        fmts = [f for f in formats if f in ALLOWED_FIRECRAWL_FORMATS] or ["markdown"]
        if "markdown" not in fmts:
            fmts.insert(0, "markdown")
        data = await self._post("scrape", {"url": url, "formats": fmts, "onlyMainContent": True})
        d = data.get("data") or {}
        meta = d.get("metadata") or {}
        text = d.get("markdown") or ""
        status = int(meta.get("statusCode") or 200)
        return RetrievedPage(
            provider=self.name, source_url=url, final_url=meta.get("url") or meta.get("sourceURL") or url,
            status=status, captured_at=now_iso(), title=meta.get("title") or "", text=text, html=d.get("html"),
            source_date=meta.get("publishedTime") or meta.get("article:published_time") or meta.get("datePublished"),
            content_sha256=sha256_text(text), image_urls=_markdown_images(text, url),
            error=None if status < 400 else f"HTTP {status}",
        )


def _markdown_images(md: str, base: str) -> list[str]:
    return [urljoin(base, m) for m in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", md)][:50]


class SearxngAdapter:
    name = "searxng"

    def __init__(self, base_url: str, timeout: float = 30, client: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.client = client

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.get(f"{self.base}/search", params={"q": query, "format": "json"}, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SearchError(f"searxng transport error: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code >= 400:
            raise SearchError(f"searxng HTTP {resp.status_code}")
        now = now_iso()
        out = []
        for i, it in enumerate(resp.json().get("results", [])[:limit]):
            if not it.get("url"):
                continue
            out.append(SearchResult(provider=self.name, rank=i + 1, title=it.get("title") or it["url"],
                                    url=canonical_url(it["url"]), snippet=it.get("content") or "", retrieved_at=now,
                                    source_date=it.get("publishedDate")))
        return out


class HttpRetrievalAdapter:
    """Plain HTTP GET with stdlib HTML extraction (no supplier AI, no cookies)."""

    name = "http"

    def __init__(self, user_agent: str, timeout: float = 30, max_bytes: int = 5_000_000,
                 client: httpx.AsyncClient | None = None):
        self.ua = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.client = client

    async def retrieve(self, url: str, formats: list[str]) -> RetrievedPage:
        client = self.client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        try:
            resp = await client.get(url, headers={"User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                                    timeout=self.timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise SearchError(f"fetch failed: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code >= 400:
            raise SearchError(f"{url} answered HTTP {resp.status_code}")
        body = resp.content[: self.max_bytes]
        ctype = resp.headers.get("content-type", "")
        final = str(resp.url)
        if "html" in ctype or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            html = body.decode(resp.encoding or "utf-8", "replace")
            ex = extract_html(html, final)
            text, title, date, images = ex["text"], ex["title"], ex["source_date"], ex["images"]
        elif ctype.startswith("text/") or "json" in ctype:
            html, text, title, date, images = None, body.decode("utf-8", "replace"), "", None, []
        else:
            raise SearchError(f"unsupported content type {ctype or 'unknown'} (use a document adapter)")
        return RetrievedPage(provider=self.name, source_url=url, final_url=final, status=resp.status_code,
                             captured_at=now_iso(), title=title, text=text, html=html if "html" in formats else None,
                             source_date=date, content_sha256=sha256_text(text), image_urls=images,
                             error=None if resp.status_code < 400 else f"HTTP {resp.status_code}")


class StaticSearchAdapter:
    """Deterministic adapter backed by a callable (fixtures and offline demos)."""

    def __init__(self, name: str, fn: Callable[[str, int], list[dict[str, Any]]]):
        self.name = name
        self.fn = fn

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        now = now_iso()
        return [SearchResult(provider=self.name, rank=i + 1, title=r["title"], url=canonical_url(r["url"]),
                             snippet=r.get("snippet", ""), retrieved_at=now, source_date=r.get("date"))
                for i, r in enumerate(self.fn(query, limit)[:limit])]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SearchService:
    def __init__(self, config: ApplicationConfig, search_adapter: SearchAdapter, retrieval_adapter: RetrievalAdapter):
        self.config = config
        self.search_adapter = search_adapter
        self.retrieval_adapter = retrieval_adapter

    @staticmethod
    def build_adapter(config: ApplicationConfig, name: str, client: httpx.AsyncClient | None = None):
        sc = config.search
        if name == "firecrawl":
            return FirecrawlAdapter(sc.firecrawl.base_url, resolve(sc.firecrawl.api_key_ref), sc.firecrawl.api_version,
                                    sc.firecrawl.timeout_seconds, client)
        if name == "searxng":
            if not sc.searxng.base_url:
                raise SearchError("search.searxng.base_url is not configured")
            return SearxngAdapter(sc.searxng.base_url, sc.searxng.timeout_seconds, client)
        if name == "http":
            return HttpRetrievalAdapter(sc.http.user_agent, sc.http.timeout_seconds, sc.http.max_bytes, client)
        raise SearchError(f"unknown search adapter {name}")

    @classmethod
    def from_config(cls, config: ApplicationConfig) -> SearchService:
        try:
            s = cls.build_adapter(config, config.search.adapter)
        except SearchError:
            s = _Unavailable(config.search.adapter)
        r = cls.build_adapter(config, config.search.retrieval_adapter) if config.search.retrieval_adapter != "firecrawl" \
            or resolve(config.search.firecrawl.api_key_ref) else cls.build_adapter(config, "http")
        return cls(config, s, r)

    def rank(self, results: list[SearchResult]) -> list[SearchResult]:
        sc = self.config.search
        seen: set[str] = set()
        scored = []
        for r in results:
            host = (urlsplit(r.url).hostname or "").lower()
            if any(host == d or host.endswith("." + d) for d in sc.block_domains):
                continue
            if r.url in seen:
                continue
            seen.add(r.url)
            score = float(r.rank)
            if any(host == d or host.endswith("." + d) for d in sc.prefer_domains):
                score -= 100
            if any(host == d or host.endswith("." + d) for d in sc.demote_domains):
                score += 100
            scored.append((score, r))
        return [r for _, r in sorted(scored, key=lambda x: x[0])]

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        return self.rank(await self.search_adapter.search(query, limit))

    async def retrieve(self, url: str, formats: list[str]) -> RetrievedPage:
        page = await self.retrieval_adapter.retrieve(url, formats)
        if page.status >= 400:
            raise SearchError(f"{url} answered HTTP {page.status}")
        if not page.text.strip():
            raise SearchError(f"{url} returned no readable text")
        return page


async def fetch_image(url: str, user_agent: str, max_bytes: int = 15_000_000,
                      client: httpx.AsyncClient | None = None) -> tuple[bytes, str]:
    """GET a public image (no cookies). Returns bytes and media type."""
    if url.startswith("data:image/"):
        import base64

        header, b64 = url.split(",", 1)
        return base64.b64decode(b64), header[5:].split(";")[0]
    c = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        resp = await c.get(url, headers={"User-Agent": user_agent, "Accept": "image/*"}, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SearchError(f"image fetch failed: {type(exc).__name__}") from exc
    finally:
        if client is None:
            await c.aclose()
    media = resp.headers.get("content-type", "").split(";")[0].strip()
    if resp.status_code >= 400:
        raise SearchError(f"image fetch returned HTTP {resp.status_code}")
    if not media.startswith("image/"):
        raise SearchError(f"{url[:80]} is not an image ({media or 'unknown type'})")
    if len(resp.content) > max_bytes:
        raise SearchError("image is too large")
    return resp.content, media


class _Unavailable:
    def __init__(self, name: str):
        self.name = name

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        raise SearchError(f"search adapter {self.name} is not configured")


def dumps_results(results: list[SearchResult]) -> str:
    return json.dumps([r.model_dump() for r in results])
