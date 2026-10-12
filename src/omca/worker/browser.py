"""Browser execution on the worker (Playwright; spec section 9, FR04-FR08).

* Attaches only to explicitly authorized sessions from the worker configuration: a CDP
  endpoint on loopback (an owner-started Chrome/Edge with a dedicated profile), a persistent
  profile directory, or an ephemeral launch (fixtures).
* Every observation assigns element references that are valid for that observation only;
  actions resolve the reference against the live page and compare the element fingerprint,
  so changed pages produce STALE_ELEMENT_REF instead of a misdirected click.
* One UI mutation at a time per session (the coordinator lease serializes tasks; a local
  lock serializes jobs).
* Every mutating action is followed by a fresh observation and verification of the
  expected result. A click receipt alone never establishes success.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import BrowserAttachment
from ..contracts import (
    ElementRef,
    ErrorCode,
    ExpectedResult,
    ImageRef,
    Observation,
    Rect,
    Viewport,
    WorkerJob,
)
from ..util import new_id, now_iso, sha256_bytes
from .agent import HandlerResult, JobContext, JobFailure

DESCRIBE_JS = r"""
const __omcaText = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
const __omcaById = (id) => { const n = document.getElementById(id); return n ? __omcaText(n) : ""; };
const __omcaRole = (el) => {
  const explicit = el.getAttribute("role"); if (explicit) return explicit.toLowerCase();
  const tag = el.tagName.toLowerCase();
  if (tag === "a") return el.hasAttribute("href") ? "link" : "generic";
  if (tag === "button" || tag === "summary") return "button";
  if (tag === "select") return el.multiple ? "listbox" : "combobox";
  if (tag === "textarea") return "textbox";
  if (tag === "img") return "img";
  if (tag === "canvas") return "canvas";
  if (tag === "video") return "video";
  if (tag === "input") {
    const t = (el.type || "text").toLowerCase();
    return ({checkbox:"checkbox", radio:"radio", range:"slider", number:"spinbutton", search:"searchbox",
             submit:"button", button:"button", reset:"button", image:"button", file:"button"})[t] || "textbox";
  }
  if (el.isContentEditable) return "textbox";
  return "generic";
};
const __omcaName = (el) => {
  const al = el.getAttribute("aria-label"); if (al && al.trim()) return al.trim();
  const lb = el.getAttribute("aria-labelledby");
  if (lb) { const t = lb.split(/\s+/).map(__omcaById).join(" ").trim(); if (t) return t; }
  if (el.labels && el.labels.length) { const t = Array.from(el.labels).map(__omcaText).join(" ").trim(); if (t) return t; }
  const tag = el.tagName.toLowerCase();
  if (tag === "img") return (el.getAttribute("alt") || "").trim();
  if (tag === "input") {
    const t = (el.type || "").toLowerCase();
    if (["submit","button","reset"].includes(t)) return (el.value || t).trim();
    if (t === "image") return (el.alt || "").trim();
    return (el.getAttribute("placeholder") || el.getAttribute("title") || el.name || "").trim();
  }
  if (tag === "textarea" || tag === "select") return (el.getAttribute("placeholder") || el.getAttribute("title") || el.name || "").trim();
  return (__omcaText(el) || el.getAttribute("title") || "").slice(0, 160);
};
const __omcaPath = (el) => { const parts = []; let n = el;
  while (n && n.nodeType === 1 && parts.length < 14) { let i = 1, s = n;
    while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
    parts.unshift(n.tagName.toLowerCase() + ":" + i); n = n.parentElement; }
  return parts.join("/"); };
const __omcaDescribe = (el) => {
  const r = el.getBoundingClientRect();
  const tag = el.tagName.toLowerCase();
  const form = el.form || el.closest("form");
  const type = tag === "input" ? (el.type || "text").toLowerCase() : (tag === "button" ? (el.getAttribute("type") || "submit").toLowerCase() : null);
  const ariaExp = el.getAttribute("aria-expanded");
  let value = null;
  if (["input","textarea","select"].includes(tag)) value = type === "password" ? (el.value ? "********" : "") : String(el.value ?? "").slice(0, 200);
  const nonText = ["checkbox","radio","submit","button","reset","image","file","range","color"];
  return {
    role: __omcaRole(el), name: __omcaName(el).slice(0, 160), tag,
    rect: {x: r.left, y: r.top, width: r.width, height: r.height},
    href: tag === "a" && el.href ? el.href : null, input_type: type,
    in_form: !!form, form_id: form ? (form.id || form.getAttribute("name") || __omcaPath(form)) : null,
    disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
    expanded: ariaExp === null ? (tag === "summary" ? !!(el.parentElement && el.parentElement.open) : null) : ariaExp === "true",
    checked: (type === "checkbox" || type === "radio") ? !!el.checked : (el.getAttribute("aria-checked") === null ? null : el.getAttribute("aria-checked") === "true"),
    value,
    editable: ((tag === "textarea") || (tag === "input" && !nonText.includes(type)) || el.isContentEditable) && !el.readOnly && !el.disabled,
    dom_path: __omcaPath(el),
  };
};
"""

OBSERVE_JS = DESCRIBE_JS + r"""
(args) => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const state = (window.__omca = window.__omca || {});
  state.obsId = args.obsId; state.refs = new Map();
  const visible = (el, r) => {
    if (r.width < 1 || r.height < 1) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) return false;
    const cs = getComputedStyle(el);
    return !(cs.visibility === "hidden" || cs.display === "none" || parseFloat(cs.opacity) === 0);
  };
  const sel = 'a[href],button,input,select,textarea,summary,[role],[onclick],[tabindex]:not([tabindex="-1"]),[contenteditable="true"],[contenteditable=""],canvas,video';
  const elements = [];
  for (const el of document.querySelectorAll(sel)) {
    if (elements.length >= args.maxElements) break;
    if (el.tagName === "INPUT" && el.type === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) continue;
    const d = __omcaDescribe(el);
    if (["generic","presentation","none"].includes(d.role) && !el.hasAttribute("onclick") && !el.hasAttribute("tabindex")) continue;
    const ref = "e" + (elements.length + 1);
    state.refs.set(ref, el);
    elements.push(Object.assign({ref}, d));
  }
  const images = [];
  if (args.includeImages) {
    for (const img of document.images) {
      if (images.length >= 40) break;
      const r = img.getBoundingClientRect();
      if (!visible(img, r) || r.width < 24 || r.height < 24) continue;
      images.push({src: img.currentSrc || img.src, alt: img.alt || "", natural_width: img.naturalWidth,
                   natural_height: img.naturalHeight, rect: {x: r.left, y: r.top, width: r.width, height: r.height}});
    }
  }
  let focused = null;
  for (const [ref, el] of state.refs) if (el === document.activeElement) focused = ref;
  const text = document.body ? document.body.innerText.slice(0, args.maxText) : "";
  return {elements, images, focused, text, url: location.href, title: document.title, vw, vh,
          dpr: window.devicePixelRatio, sx: window.scrollX, sy: window.scrollY,
          dom: document.getElementsByTagName("*").length};
}
"""

RESOLVE_JS = DESCRIBE_JS + r"""
(args) => {
  const s = window.__omca;
  if (!s || s.obsId !== args.obsId) return {status: "stale_observation"};
  const el = s.refs.get(args.ref);
  if (!el) return {status: "unknown_ref"};
  if (!el.isConnected) return {status: "detached"};
  return {status: "ok", desc: __omcaDescribe(el)};
}
"""

ELEMENT_JS = "(args) => window.__omca && window.__omca.obsId === args.obsId ? window.__omca.refs.get(args.ref) : null"

HIT_TEST_JS = DESCRIBE_JS + r"""
(p) => {
  let el = document.elementFromPoint(p.x, p.y);
  if (!el) return null;
  const interactive = el.closest('a[href],button,input,select,textarea,summary,[role],[onclick],canvas,video,[contenteditable="true"]');
  el = interactive || el;
  return __omcaDescribe(el);
}
"""

VIDEO_INFO_JS = r"""
() => Array.from(document.querySelectorAll("video")).map((v, i) => ({index: i, duration: v.duration,
  src: v.currentSrc || v.src || "", width: v.videoWidth, height: v.videoHeight, paused: v.paused,
  rect: (() => { const r = v.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; })()}))
"""

SEEK_JS = r"""
async (args) => {
  const v = document.querySelectorAll("video")[args.index];
  if (!v) return {ok: false, error: "no video"};
  if (!isFinite(v.duration) || v.duration === 0) {
    await new Promise((res) => { v.addEventListener("loadedmetadata", res, {once: true}); setTimeout(res, 5000); });
  }
  v.pause();
  const t = Math.max(0, Math.min(args.t, (isFinite(v.duration) ? v.duration : args.t) - 0.05));
  await new Promise((res) => { v.addEventListener("seeked", res, {once: true}); v.currentTime = t; setTimeout(res, 5000); });
  return {ok: true, t: v.currentTime, duration: v.duration};
}
"""


def fingerprint(desc: dict[str, Any]) -> str:
    key = "|".join(str(desc.get(k) or "") for k in ("role", "name", "tag", "href", "input_type", "form_id", "dom_path"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def element_from_desc(ref: str, desc: dict[str, Any]) -> ElementRef:
    return ElementRef(
        ref=ref, role=desc["role"], name=desc.get("name") or "", tag=desc["tag"], rect=Rect(**desc["rect"]),
        href=desc.get("href"), input_type=desc.get("input_type"), in_form=bool(desc.get("in_form")),
        form_id=desc.get("form_id"), disabled=bool(desc.get("disabled")), expanded=desc.get("expanded"),
        checked=desc.get("checked"), value=desc.get("value"), editable=bool(desc.get("editable")),
        fingerprint=fingerprint(desc),
    )


@dataclass
class BrowserSession:
    attachment: BrowserAttachment
    session_id: str
    context: Any
    browser: Any = None
    pages: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    new_pages: list[Any] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)


class BrowserManager:
    def __init__(self, attachments: list[BrowserAttachment], max_text_chars: int = 20000):
        self.attachments = {a.id: a for a in attachments}
        self.max_text_chars = max_text_chars
        self._pw = None
        self._sessions: dict[str, BrowserSession] = {}

    async def _playwright(self):
        if self._pw is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
        return self._pw

    async def session(self, attachment_id: str | None) -> BrowserSession:
        if attachment_id is None:
            if len(self.attachments) != 1:
                raise JobFailure(ErrorCode.AUTH_REQUIRED, "job does not name a browser attachment")
            attachment_id = next(iter(self.attachments))
        att = self.attachments.get(attachment_id)
        if att is None:
            raise JobFailure(ErrorCode.AUTH_REQUIRED, f"browser attachment {attachment_id} is not configured on this worker")
        if not att.authorized:
            raise JobFailure(ErrorCode.AUTH_REQUIRED, f"browser attachment {attachment_id} is not authorized by the owner")
        existing = self._sessions.get(att.id)
        if existing is not None:
            return existing
        pw = await self._playwright()
        browser = None
        try:
            if att.mode == "cdp":
                browser = await pw.chromium.connect_over_cdp(att.cdp_url)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            elif att.mode == "persistent_profile":
                if not att.user_data_dir:
                    raise JobFailure(ErrorCode.AUTH_REQUIRED, f"attachment {att.id} has no user_data_dir")
                kwargs: dict[str, Any] = {"headless": att.headless, "accept_downloads": True}
                if att.channel != "chromium":
                    kwargs["channel"] = att.channel
                if att.viewport:
                    kwargs["viewport"] = {"width": att.viewport[0], "height": att.viewport[1]}
                context = await pw.chromium.launch_persistent_context(att.user_data_dir, **kwargs)
            else:
                kwargs = {"headless": att.headless}
                if att.channel != "chromium":
                    kwargs["channel"] = att.channel
                browser = await pw.chromium.launch(**kwargs)
                ctx_kwargs: dict[str, Any] = {"accept_downloads": True}
                if att.viewport:
                    ctx_kwargs["viewport"] = {"width": att.viewport[0], "height": att.viewport[1]}
                if att.device_scale_factor:
                    ctx_kwargs["device_scale_factor"] = att.device_scale_factor
                context = await browser.new_context(**ctx_kwargs)
        except JobFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise JobFailure(ErrorCode.WORKER_UNAVAILABLE, f"cannot attach to browser {att.id}: {exc}") from exc
        sess = BrowserSession(att, new_id("bss"), context, browser)
        context.on("page", lambda p: sess.new_pages.append(p))
        self._sessions[att.id] = sess
        return sess

    async def release_session(self, attachment_id: str) -> None:
        """Close this worker's browser for an attachment so another program can open its profile."""
        sess = self._sessions.pop(attachment_id, None)
        if sess is None:
            return
        try:
            if sess.attachment.mode == "cdp":
                for p in sess.pages.values():
                    if not p.is_closed():
                        await p.close()
            elif sess.browser is not None:
                await sess.browser.close()
            else:
                await sess.context.close()
        except Exception:  # noqa: BLE001 - already closed
            pass

    async def page(self, sess: BrowserSession, task_id: str):
        page = sess.pages.get(task_id)
        if page is None or page.is_closed():
            page = await sess.context.new_page()
            sess.new_pages = [p for p in sess.new_pages if p is not page]
            sess.pages[task_id] = page
        return page

    async def close(self) -> None:
        for s in self._sessions.values():
            try:
                if s.attachment.mode == "cdp":
                    for p in s.pages.values():
                        if not p.is_closed():
                            await p.close()
                elif s.browser is not None:
                    await s.browser.close()
                else:
                    await s.context.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    # -- observation ------------------------------------------------------------------------------
    async def observe(self, sess: BrowserSession, page, ctx: JobContext, *, include_images: bool = True,
                      max_elements: int = 250, full_page: bool = False) -> tuple[Observation, list[str]]:
        obs_id = new_id("obs")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:  # noqa: BLE001 - observe whatever is there
            pass
        data = await page.evaluate(OBSERVE_JS, {"obsId": obs_id, "maxElements": max_elements,
                                                "includeImages": include_images, "maxText": self.max_text_chars})
        shot = await page.screenshot(type="png", full_page=full_page)
        import io

        from PIL import Image

        with Image.open(io.BytesIO(shot)) as im:
            sw, sh = im.size
        art = await ctx.upload("screenshot", f"{obs_id}.png", shot, media_type="image/png",
                               meta={"width": sw, "height": sh, "observation_id": obs_id})
        elements = [element_from_desc(e["ref"], e) for e in data["elements"]]
        images = [ImageRef(src=i["src"], alt=i["alt"], natural_width=i["natural_width"],
                           natural_height=i["natural_height"], rect=Rect(**i["rect"])) for i in data["images"]]
        tabs = [p.url for p in sess.context.pages if not p.is_closed()]
        obs = Observation(
            observation_id=obs_id, task_id=ctx.job.task_id, worker_id=ctx.job.worker_id, session_id=sess.session_id,
            url=data["url"], title=data["title"], captured_at=now_iso(),
            viewport=Viewport(width=data["vw"], height=data["vh"], device_pixel_ratio=data["dpr"],
                              scroll_x=data["sx"], scroll_y=data["sy"]),
            elements=elements, focused_ref=data["focused"], text=data["text"], screenshot_artifact_id=art,
            screenshot_sha256=sha256_bytes(shot), screenshot_width=sw, screenshot_height=sh, images=images,
            open_tabs=tabs, downloads=list(sess.downloads), dom_version=str(data["dom"]),
        )
        return obs, [art]

    async def resolve(self, page, observation_id: str | None, ref: str, expected_fp: str | None):
        if not observation_id:
            raise JobFailure(ErrorCode.STALE_OBSERVATION, "element actions need the observation id")
        res = await page.evaluate(RESOLVE_JS, {"obsId": observation_id, "ref": ref})
        if res.get("status") != "ok":
            raise JobFailure(ErrorCode.STALE_ELEMENT_REF, f"element {ref} is no longer valid ({res.get('status')})",
                             retryable=True)
        fp = fingerprint(res["desc"])
        if expected_fp and fp != expected_fp:
            raise JobFailure(ErrorCode.STALE_ELEMENT_REF, f"element {ref} changed since it was observed", retryable=True)
        handle = await page.evaluate_handle(ELEMENT_JS, {"obsId": observation_id, "ref": ref})
        el = handle.as_element()
        if el is None:
            raise JobFailure(ErrorCode.STALE_ELEMENT_REF, f"element {ref} is no longer attached", retryable=True)
        return el, res["desc"]

    async def settle(self, sess: BrowserSession, page, task_id: str):
        """Let navigation finish and adopt a newly opened tab."""
        await asyncio.sleep(0.25)
        new_tab = False
        if sess.new_pages:
            newest = sess.new_pages.pop()
            sess.new_pages.clear()
            if not newest.is_closed():
                sess.pages[task_id] = newest
                page = newest
                new_tab = True
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:  # noqa: BLE001
            pass
        return page, new_tab


def verify_expected(expected: ExpectedResult | None, obs: Observation, new_tab: bool,
                    download: str | None) -> dict[str, Any]:
    if expected is None:
        return {"checked": False}
    checks: dict[str, bool] = {}
    if expected.url_contains is not None:
        checks["url_contains"] = expected.url_contains in obs.url
    if expected.text_visible is not None:
        checks["text_visible"] = expected.text_visible.lower() in obs.text.lower()
    if expected.element_value:
        for name, value in expected.element_value.items():
            el = next((e for e in obs.elements if e.name == name or e.ref == name), None)
            checks[f"value:{name}"] = el is not None and (el.value or "") == value
    if expected.download_name is not None:
        checks["download"] = bool(download) and expected.download_name.lower() in (download or "").lower()
    if expected.new_tab is not None:
        checks["new_tab"] = expected.new_tab == new_tab
    return {"checked": True, "checks": checks, "passed": all(checks.values()) if checks else True}


def make_browser_handlers(manager: BrowserManager, *, allowed_extensions: list[str] | None = None,
                          max_download_bytes: int = 500 * 1024 * 1024) -> dict[str, Any]:
    allowed_ext = {e.lower() for e in (allowed_extensions or [])}

    async def _common(job: WorkerJob, ctx: JobContext):
        sess = await manager.session(job.attachment_id)
        page = await manager.page(sess, job.task_id)
        return sess, page

    async def _after(sess, page, job: WorkerJob, ctx: JobContext, result: dict[str, Any], download: str | None = None,
                     extra_artifacts: list[str] | None = None) -> HandlerResult:
        page, new_tab = await manager.settle(sess, page, job.task_id)
        obs, arts = await manager.observe(sess, page, ctx)
        verification = verify_expected(job.expected, obs, new_tab, download)
        result = {**result, "new_tab": new_tab, "url": obs.url}
        return HandlerResult(result=result, observation=obs, artifact_ids=[*(extra_artifacts or []), *arts],
                             verification=verification)

    async def observe(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            a = job.arguments
            obs, arts = await manager.observe(sess, page, ctx, include_images=a.get("include_images", True),
                                              max_elements=a.get("max_elements", 250), full_page=a.get("full_page", False))
        return HandlerResult(result={"url": obs.url}, observation=obs, artifact_ids=arts)

    async def read_page(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            url = job.arguments.get("url")
            if url:
                await _goto(page, url)
            obs, arts = await manager.observe(sess, page, ctx)
        return HandlerResult(result={"url": obs.url, "title": obs.title, "text": obs.text}, observation=obs,
                             artifact_ids=arts)

    async def _goto(page, url: str) -> int | None:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            raise JobFailure(ErrorCode.SITE_UNAVAILABLE, f"could not open {url}: {exc}"[:300], retryable=True) from exc
        status = resp.status if resp else None
        if status is not None and status >= 400:
            raise JobFailure(ErrorCode.SITE_UNAVAILABLE, f"{url} answered HTTP {status}", retryable=status >= 500)
        return status

    async def navigate(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            status = await _goto(page, job.arguments["url"])
            return await _after(sess, page, job, ctx, {"http_status": status})

    async def back(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            await page.go_back(wait_until="domcontentloaded", timeout=30000)
            return await _after(sess, page, job, ctx, {})

    async def click(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            el, desc = await manager.resolve(page, job.arguments.get("observation_id") or job.arguments.get("_obs"),
                                             job.arguments["ref"], job.arguments.get("_fingerprint"))
            try:
                await el.click(timeout=8000)
            except Exception as exc:  # noqa: BLE001
                raise JobFailure(ErrorCode.VERIFICATION_FAILED, f"element not actionable: {exc}"[:300],
                                 retryable=True) from exc
            return await _after(sess, page, job, ctx, {"clicked": desc.get("name")})

    async def hit_test(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            desc = await page.evaluate(HIT_TEST_JS, {"x": job.arguments["x"], "y": job.arguments["y"]})
        if desc is None:
            return HandlerResult(result={"element": None})
        return HandlerResult(result={"element": element_from_desc("hit", desc).model_dump()})

    async def click_point(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        x, y = float(job.arguments["x"]), float(job.arguments["y"])
        async with sess.lock:
            expect = job.arguments.get("expect_fingerprint")
            if expect:
                desc = await page.evaluate(HIT_TEST_JS, {"x": x, "y": y})
                if desc is None or fingerprint(desc) != expect:
                    raise JobFailure(ErrorCode.STALE_ELEMENT_REF, "the element under the click point changed",
                                     retryable=True)
            await page.mouse.click(x, y)
            return await _after(sess, page, job, ctx, {"clicked_at": [x, y]})

    async def type_text(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        a = job.arguments
        async with sess.lock:
            el, desc = await manager.resolve(page, a.get("_obs"), a["ref"], a.get("_fingerprint"))
            if desc.get("input_type") == "password":
                raise JobFailure(ErrorCode.POLICY_DENIED, "refusing to type into a password field")
            try:
                if a.get("clear", True):
                    await el.fill(a["text"], timeout=8000)
                else:
                    await el.type(a["text"], timeout=8000)
                if a.get("submit"):
                    await el.press("Enter")
            except Exception as exc:  # noqa: BLE001
                raise JobFailure(ErrorCode.VERIFICATION_FAILED, f"could not type: {exc}"[:300], retryable=True) from exc
            return await _after(sess, page, job, ctx, {"typed_chars": len(a["text"])})

    async def press(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            key = job.arguments["key"]
            await page.keyboard.press("Space" if key == " " else key)
            return await _after(sess, page, job, ctx, {"key": key})

    async def select(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        a = job.arguments
        async with sess.lock:
            el, _ = await manager.resolve(page, a.get("_obs"), a["ref"], a.get("_fingerprint"))
            try:
                await el.select_option(a["value"], timeout=8000)
            except Exception:  # noqa: BLE001 - try by label
                await el.select_option(label=a["value"], timeout=8000)
            return await _after(sess, page, job, ctx, {"selected": a["value"]})

    async def scroll(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            await page.mouse.wheel(job.arguments.get("dx", 0), job.arguments.get("dy", 600))
            return await _after(sess, page, job, ctx, {})

    async def wait(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        async with sess.lock:
            text = job.arguments.get("text")
            ms = job.arguments.get("ms", 1000)
            found = None
            if text:
                try:
                    await page.get_by_text(text).first.wait_for(timeout=ms or 1000)
                    found = True
                except Exception:  # noqa: BLE001
                    found = False
            else:
                await page.wait_for_timeout(ms)
            return await _after(sess, page, job, ctx, {"text_found": found})

    async def fetch_image(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        src = job.arguments["src"]
        name = job.arguments.get("name") or "image"
        if src.startswith("data:image/"):
            import base64

            header, b64 = src.split(",", 1)
            data = base64.b64decode(b64)
            media = header[5:].split(";")[0]
        else:
            try:
                resp = await sess.context.request.get(src, timeout=30000)
            except Exception as exc:  # noqa: BLE001
                raise JobFailure(ErrorCode.SITE_UNAVAILABLE, f"image fetch failed: {exc}"[:300], retryable=True) from exc
            if resp.status >= 400:
                raise JobFailure(ErrorCode.SITE_UNAVAILABLE, f"image fetch returned HTTP {resp.status}")
            data = await resp.body()
            media = (resp.headers.get("content-type") or "").split(";")[0]
        if not media.startswith("image/"):
            raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, f"{src[:80]} is not an image ({media})")
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(media)
        if ext is None:
            raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, f"unsupported image type {media}")
        art = await ctx.upload("image", f"{name}{ext}", data, media_type=media, meta={"source_url": src})
        return HandlerResult(result={"artifact_id": art, "bytes": len(data), "source_url": src}, artifact_ids=[art])

    async def download(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        a = job.arguments
        async with sess.lock:
            el, _ = await manager.resolve(page, a.get("_obs"), a["ref"], a.get("_fingerprint"))
            try:
                async with page.expect_download(timeout=60000) as info:
                    await el.click(timeout=8000)
                dl = await info.value
            except Exception as exc:  # noqa: BLE001
                raise JobFailure(ErrorCode.VERIFICATION_FAILED, f"no download started: {exc}"[:300]) from exc
            name = dl.suggested_filename or "download.bin"
            ext = os.path.splitext(name)[1].lower()
            if allowed_ext and ext not in allowed_ext:
                await dl.cancel()
                raise JobFailure(ErrorCode.POLICY_DENIED, f"download type {ext or '(none)'} is not allowed")
            tmp = Path(ctx.workspace) / "downloads" / f"{job.action_id}{ext}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            await dl.save_as(str(tmp))
            size = tmp.stat().st_size
            if size > max_download_bytes:
                tmp.unlink(missing_ok=True)
                raise JobFailure(ErrorCode.POLICY_DENIED, f"download is {size} bytes (limit {max_download_bytes})")
            art = await ctx.upload("download", name, tmp.read_bytes(), meta={"source_url": dl.url})
            tmp.unlink(missing_ok=True)
            sess.downloads.append(name)
            return await _after(sess, page, job, ctx, {"download": name, "artifact_id": art, "bytes": size},
                                download=name, extra_artifacts=[art])

    async def video_frames(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess, page = await _common(job, ctx)
        a = job.arguments
        async with sess.lock:
            videos = await page.evaluate(VIDEO_INFO_JS)
            if not videos:
                raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, "the page has no playable video element")
            idx = 0
            loc = page.locator("video").nth(idx)
            frames = []
            for t in a["timestamps"]:
                res = await page.evaluate(SEEK_JS, {"index": idx, "t": float(t)})
                if not res.get("ok"):
                    raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, f"cannot seek video: {res.get('error')}")
                shot = await loc.screenshot(type="png", timeout=15000)
                art = await ctx.upload("frame", f"frame_{float(res['t']):09.3f}.png", shot, media_type="image/png",
                                       meta={"timestamp": res["t"], "source_url": page.url, "method": "playback"})
                frames.append({"timestamp": res["t"], "artifact_id": art})
            return HandlerResult(result={"frames": frames, "duration": videos[idx].get("duration"),
                                         "video_src": videos[idx].get("src"), "page_url": page.url},
                                 artifact_ids=[f["artifact_id"] for f in frames])

    async def release(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        sess = await manager.session(job.attachment_id)
        page = sess.pages.pop(job.task_id, None)
        if page is not None and not page.is_closed():
            await page.close()
        return HandlerResult(result={"released": True})

    return {
        "browser.observe": observe,
        "browser.read_page": read_page,
        "browser.navigate": navigate,
        "browser.back": back,
        "browser.click": click,
        "browser.hit_test": hit_test,
        "browser.click_point": click_point,
        "browser.type": type_text,
        "browser.press": press,
        "browser.select": select,
        "browser.scroll": scroll,
        "browser.wait": wait,
        "browser.fetch_image": fetch_image,
        "browser.download": download,
        "browser.video_frames": video_frames,
        "browser.release": release,
    }
