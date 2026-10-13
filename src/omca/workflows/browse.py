"""Generic browser task: observe -> open vision model chooses one action -> policy -> execute ->
reobserve and verify (spec section 9, figure 4; FR04-FR08, FR11).

Element references are preferred; vision-guided coordinates are a fallback for canvas or
visually identified controls and are mapped back to CSS pixels through the recorded image
resize transform and device pixel ratio. Success is judged from a fresh observation, never
from the model's own claim or a click receipt.
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts import ExpectedResult, Observation
from ..credentials import describe as describe_credentials
from ..engine import ActionOutcome, WorkflowOutcome
from ..evidence import locate_excerpt
from ..models.registry import RouteRejected
from ..models.types import ImageInput, ImageTransform
from ..util import now_iso, parse_iso, truncate, utcnow
from .common import model_json, system_prompt, untrusted

URL = re.compile(r"https?://[^\s<>\"']+")


def tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


EXPECT = {"expect_url_contains": {"type": "string"}, "expect_text": {"type": "string"}}
TOOLS = [
    tool("click", "Click an element by its reference from the element list.", {"ref": {"type": "string"}, **EXPECT}, ["ref"]),
    tool("click_point", "Click a point in the screenshot when no element reference fits (canvas, icons).",
         {"x": {"type": "number"}, "y": {"type": "number"}, **EXPECT}, ["x", "y"]),
    tool("type", "Type text into a text field by reference.",
         {"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["ref", "text"]),
    tool("press", "Press a key such as Enter, Tab, Escape, PageDown.", {"key": {"type": "string"}}, ["key"]),
    tool("select", "Choose an option in a select element.", {"ref": {"type": "string"}, "value": {"type": "string"}},
         ["ref", "value"]),
    tool("scroll", "Scroll the page.", {"direction": {"type": "string", "enum": ["up", "down"]}}, ["direction"]),
    tool("navigate", "Open a URL.", {"url": {"type": "string"}}, ["url"]),
    tool("back", "Go back in history.", {}, []),
    tool("wait", "Wait for the page (milliseconds) or for text to appear.",
         {"ms": {"type": "integer"}, "text": {"type": "string"}}, []),
    tool("record_evidence", "Record a fact visible on the page with an exact quote from the page text.",
         {"statement": {"type": "string"}, "quote": {"type": "string"}}, ["statement", "quote"]),
    tool("ask_owner", "Ask the owner for missing information or a manual login step.",
         {"question": {"type": "string"}}, ["question"]),
    tool("finish", "Stop: the goal is achieved or cannot be achieved.",
         {"success": {"type": "boolean"}, "summary": {"type": "string"}}, ["success", "summary"]),
]


def model_point_to_css(x: float, y: float, transform: ImageTransform, coordinate_system: str,
                       device_pixel_ratio: float) -> tuple[float, float]:
    """Map a point the model gave on the (possibly resized) image back to viewport CSS pixels."""
    if coordinate_system == "normalized_1000":
        px, py = x / 1000.0 * transform.sent_width, y / 1000.0 * transform.sent_height
    elif coordinate_system == "normalized_1":
        px, py = x * transform.sent_width, y * transform.sent_height
    else:
        px, py = x, y
    scale = transform.scale or 1.0
    ox, oy = px / scale, py / scale  # original screenshot pixels
    dpr = device_pixel_ratio or 1.0
    return ox / dpr, oy / dpr


def describe_elements(obs: Observation, limit: int = 120) -> str:
    lines = []
    for e in obs.elements[:limit]:
        bits = [e.ref, e.role, f'"{truncate(e.name, 70)}"']
        if e.href:
            bits.append(f"-> {truncate(e.href, 80)}")
        if e.value:
            bits.append(f'value="{truncate(e.value, 40)}"')
        if e.in_form:
            bits.append("[in form]")
        if e.disabled:
            bits.append("[disabled]")
        if e.expanded is not None:
            bits.append(f"[expanded={e.expanded}]")
        if e.ref == obs.focused_ref:
            bits.append("[focused]")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _approval_hint(cfg) -> str:
    if cfg.policy.auto_approve_browser:
        return "Browser actions run without asking the owner, so double-check anything irreversible. "
    return "Submissions, sends, purchases and deletions will be shown to the owner for approval. "


def _login_hint(cfg) -> str:
    if not cfg.browser.credentials:
        return "Never type passwords; use ask_owner for a manual login."
    return ("To sign in, type these placeholders with the type tool (the real values are filled in on the matching "
            "site only; never invent credentials):\n" + describe_credentials(cfg.browser.credentials)
            + "\nFor any other login, use ask_owner.")


def coordinate_hint(cs: str) -> str:
    return {"normalized_1000": "Coordinates for click_point use a 0-1000 scale of the screenshot width and height.",
            "normalized_1": "Coordinates for click_point are fractions 0-1 of the screenshot width and height.",
            "pixels": "Coordinates for click_point are pixels of the screenshot image you see."}[cs]


async def browser_task(ctx) -> WorkflowOutcome:
    c, cfg = ctx.c, ctx.config
    req = ctx.task.request
    att = c.attachment(req.params.get("attachment_id")) or c.default_attachment(req.worker_id)
    if att is None or not att.authorized:
        return WorkflowOutcome("FAILED", "No explicitly authorized browser session is configured for this task.",
                               error_code="AUTH_REQUIRED")
    try:
        route = c.registry.chain("vision", {"text", "image"})[0]
    except RouteRejected as exc:
        return WorkflowOutcome("FAILED", f"No usable open vision route: {exc}", error_code="ROUTE_REJECTED")
    start = req.params.get("url") or (URL.search(ctx.task.goal).group(0) if URL.search(ctx.task.goal) else None)
    if cfg.browser.engine == "browser_use":
        return await browser_use_task(ctx, att, start)
    max_steps = int(req.params["max_steps"]) if req.params.get("max_steps") else None  # None: no step cap

    if start:
        first = await ctx.act("browser.navigate", {"url": start}, key="nav:0", attachment_id=att.id)
        if not first.ok:
            return WorkflowOutcome("FAILED", f"Could not open {start}: {first.message or first.reason}",
                                   limitations=[f"site unavailable or blocked: {start}"], error_code=first.error_code or "SITE_UNAVAILABLE")
        obs = first.obs
    else:
        obs = (await ctx.act("browser.observe", {}, key="obs:0", attachment_id=att.id)).obs
    history: list[str] = []
    evidence_ids: list[str] = []
    finished: dict[str, Any] | None = None
    malformed = 0

    step = 0
    while max_steps is None or step < max_steps:
        step += 1
        obs = await _fresh(ctx, obs, step, att.id)
        if obs is None:
            return WorkflowOutcome("FAILED", "The browser session produced no observation.", error_code="WORKER_UNAVAILABLE")
        ctx.progress(f"step {step}: {truncate(obs.title or obs.url, 80)}", phase="browse")
        shot = c.artifacts.read_bytes(obs.screenshot_artifact_id) if obs.screenshot_artifact_id else None
        prompt = (
            f"Goal: {ctx.task.goal}\n\nCurrent page: {obs.url}\nTitle: {obs.title}\n"
            f"Viewport {obs.viewport.width}x{obs.viewport.height}. {coordinate_hint(route.coordinate_system)}\n\n"
            f"Interactive elements (ref role name ...):\n{describe_elements(obs)}\n\n"
            + untrusted(obs.url, obs.text, 6000)
            + "\n\nHistory:\n" + ("\n".join(history[-12:]) or "(none)")
            + "\n\nChoose exactly one tool call. Prefer element references; use click_point only for canvas or "
              "visual-only controls. " + _approval_hint(cfg) + _login_hint(cfg)
        )
        resp = await ctx.model("vision", [
            {"role": "system", "content": system_prompt(ctx, "You operate a web browser for the owner, one action at a time.")},
            {"role": "user", "content": prompt},
        ], key=f"decide:{step}", images=[ImageInput(shot, "image/png", obs.screenshot_artifact_id)] if shot else None,
            tools=TOOLS, tool_choice="auto", max_output_tokens=600)
        call = next((t for t in resp.tool_calls if t.arguments is not None), None)
        if call is None:
            malformed += 1
            history.append(f"{step}. (no valid tool call: {truncate(resp.text, 80)})")
            if malformed >= 2:
                ctx.note_limitation("the model did not return a valid tool call (MALFORMED_TOOL_CALL)")
                break
            continue
        a = call.arguments or {}
        expected = None
        if a.get("expect_url_contains") or a.get("expect_text"):
            expected = ExpectedResult(url_contains=a.get("expect_url_contains"), text_visible=a.get("expect_text"))
        out: ActionOutcome | None = None
        key = f"act:{step}"
        name = call.name
        if name == "finish":
            finished = {"success": bool(a.get("success")), "summary": str(a.get("summary") or "")}
            break
        if name == "record_evidence":
            quote = str(a.get("quote") or "")
            if locate_excerpt(obs.text, quote):
                ev = c.evidence.record(ctx.task_id, kind="text_excerpt", source_url=obs.url, final_url=obs.url,
                                       captured_at=obs.captured_at, excerpt=quote, content_for_hash=obs.text,
                                       locator={"observation_id": obs.observation_id}, provider=f"browser:{att.id}",
                                       image_artifact_id=obs.screenshot_artifact_id, stable_key=f"browse:{step}")
                evidence_ids.append(ev.evidence_id)
                history.append(f"{step}. recorded evidence: {truncate(str(a.get('statement')), 100)}")
            else:
                history.append(f"{step}. evidence rejected: quote not found on the page")
            continue
        if name == "ask_owner":
            answer = await ctx.ask_owner(str(a.get("question") or "Please advise."), key=f"q:{step}")
            history.append(f"{step}. asked owner; answer: {truncate(answer or '(no answer)', 200)}")
            # the page may have changed while waiting (e.g. a manual login), so observe it again
            obs = (await ctx.act("browser.observe", {}, key=f"obs:after-ask:{step}", attachment_id=att.id)).obs
            continue
        if name == "click":
            out = await ctx.act("browser.click", {"ref": str(a.get("ref"))}, key=key, observation=obs, expected=expected,
                                attachment_id=att.id)
        elif name == "click_point":
            transform = resp.image_transforms[0] if resp.image_transforms else ImageTransform(
                obs.screenshot_artifact_id, obs.screenshot_width or 0, obs.screenshot_height or 0,
                obs.screenshot_width or 0, obs.screenshot_height or 0)
            x, y = model_point_to_css(float(a.get("x", 0)), float(a.get("y", 0)), transform, route.coordinate_system,
                                      obs.viewport.device_pixel_ratio)
            out = await ctx.act("browser.click_point", {"x": round(x, 1), "y": round(y, 1)}, key=key, observation=obs,
                                expected=expected, attachment_id=att.id)
        elif name == "type":
            out = await ctx.act("browser.type", {"ref": str(a.get("ref")), "text": str(a.get("text", "")),
                                                 "submit": bool(a.get("submit", False))}, key=key, observation=obs,
                                attachment_id=att.id)
        elif name == "press":
            out = await ctx.act("browser.press", {"key": str(a.get("key"))}, key=key, observation=obs, attachment_id=att.id)
        elif name == "select":
            out = await ctx.act("browser.select", {"ref": str(a.get("ref")), "value": str(a.get("value", ""))}, key=key,
                                observation=obs, attachment_id=att.id)
        elif name == "scroll":
            dy = -600 if a.get("direction") == "up" else 600
            out = await ctx.act("browser.scroll", {"dy": dy}, key=key, observation=obs, attachment_id=att.id)
        elif name == "navigate":
            out = await ctx.act("browser.navigate", {"url": str(a.get("url"))}, key=key, attachment_id=att.id)
        elif name == "back":
            out = await ctx.act("browser.back", {}, key=key, attachment_id=att.id)
        elif name == "wait":
            out = await ctx.act("browser.wait", {"ms": min(int(a.get("ms") or 1000), 15000), "text": a.get("text")},
                                key=key, attachment_id=att.id)
        else:
            history.append(f"{step}. unknown tool {name}")
            continue
        history.append(f"{step}. {name} {truncate(str(a), 120)} -> {_describe(out)}")
        if out.obs is not None:
            obs = out.obs
        elif out.error_code in ("STALE_ELEMENT_REF", "STALE_OBSERVATION"):
            obs = None

    # verify the end state from a fresh observation, not from the model's claim
    final_obs = await _fresh(ctx, None, step + 1, att.id)
    verdict = {"achieved": False, "evidence": "not checked"}
    if final_obs is not None:
        shot = c.artifacts.read_bytes(final_obs.screenshot_artifact_id) if final_obs.screenshot_artifact_id else None
        verdict = await model_json(ctx, "review", [
            {"role": "user", "content": (
                f"Goal: {ctx.task.goal}\nCurrent page: {final_obs.url}\n" + untrusted(final_obs.url, final_obs.text, 6000)
                + "\n\nJudging only from this page state and screenshot, is the goal achieved? Return JSON "
                  "{\"achieved\": bool, \"evidence\": \"what on the page shows it\"}")},
        ], key="verify", images=[ImageInput(shot, "image/png")] if shot else None, default=verdict) or verdict
    claimed = finished or {"success": False, "summary": "stopped at the step limit"}
    achieved = bool(verdict.get("achieved")) and bool(claimed.get("success"))
    summary = (f"{claimed.get('summary')}\nVerified from the page: {'yes' if achieved else 'no'} - "
               f"{truncate(str(verdict.get('evidence')), 300)}\nFinal page: {final_obs.url if final_obs else '-'}")
    if not achieved:
        ctx.note_limitation("the goal could not be confirmed from the final page state")
    delivered = await ctx.act("telegram.send_owner", {"text": truncate(summary, 3500)}, key="deliver")
    return WorkflowOutcome("COMPLETED" if achieved else "PARTIAL", summary, limitations=list(ctx.limitations),
                           details={"steps": len(history), "evidence": evidence_ids, "verdict": verdict,
                                    "delivered": delivered.ok},
                           checks_passed=achieved and delivered.ok)


async def browser_use_task(ctx, att, start: str | None) -> WorkflowOutcome:
    """Hand the whole goal to the Browser Use agent, then judge the result from its final page and screenshot."""
    c = ctx.c
    req = ctx.task.request
    args: dict[str, Any] = {"goal": ctx.task.goal, "start_url": start}
    if req.params.get("max_steps"):
        args["max_steps"] = int(req.params["max_steps"])
    ctx.progress("Browser Use agent is operating the browser", phase="browse")
    out = await ctx.act("browser.agent_task", args, key="agent", attachment_id=att.id,
                        preview=f"Browser Use agent: {truncate(ctx.task.goal, 200)}", retries=0)
    if not out.ok:
        return WorkflowOutcome("FAILED", f"The Browser Use agent did not finish: {_describe(out)}",
                               limitations=list(ctx.limitations), error_code=out.error_code or "INTERNAL")
    r = out.result
    shot_id = r.get("screenshot_artifact_id")
    shot = c.artifacts.read_bytes(shot_id) if shot_id else None
    final_url = r.get("url") or "-"
    verdict = {"achieved": False, "evidence": "not checked"}
    if shot or r.get("text"):
        verdict = await model_json(ctx, "review", [
            {"role": "user", "content": (
                f"Goal: {ctx.task.goal}\n"
                f"Pages the agent visited, in order: {', '.join((r.get('urls') or [])[-8:]) or '-'}\n"
                f"The agent's reported result: {truncate(str(r.get('final_result') or ''), 1500)}\n\n"
                f"Final page: {final_url}\n" + untrusted(final_url, r.get("text") or "", 6000)
                + "\n\nThe goal may have needed several steps, so earlier pages need not still be visible. Judging "
                  "from this final page and screenshot, does the page state support the reported result, and is it "
                  "consistent with the goal being achieved? Do not accept a result the page contradicts. Return "
                  "JSON {\"achieved\": bool, \"evidence\": \"what on the page shows it\"}")},
        ], key="verify", images=[ImageInput(shot, "image/png", shot_id)] if shot else None, default=verdict) or verdict
    claimed = bool(r.get("success")) if r.get("done") else False
    achieved = claimed and bool(verdict.get("achieved"))
    if not r.get("done"):
        ctx.note_limitation(f"the agent stopped after {r.get('steps')} steps without finishing")
    if not (r.get("done") and r.get("success")):
        for err in (r.get("errors") or [])[-3:]:
            ctx.note_limitation(f"agent error: {truncate(str(err), 160)}")
    if not achieved:
        ctx.note_limitation("the goal could not be confirmed from the final page state")
    summary = (f"{r.get('final_result') or 'The agent returned no summary.'}\n"
               f"Verified from the page: {'yes' if achieved else 'no'} - {truncate(str(verdict.get('evidence')), 300)}\n"
               f"Final page: {final_url} ({r.get('steps')} steps)")
    delivered = await ctx.act("telegram.send_owner", {"text": truncate(summary, 3500)}, key="deliver")
    return WorkflowOutcome("COMPLETED" if achieved else "PARTIAL", summary, limitations=list(ctx.limitations),
                           details={"engine": "browser_use", "steps": r.get("steps"), "urls": r.get("urls"),
                                    "screenshot_artifact_id": shot_id, "final_url": r.get("url"),
                                    "verdict": verdict, "delivered": delivered.ok},
                           checks_passed=achieved and delivered.ok)


async def _fresh(ctx, obs: Observation | None, step: int, attachment_id: str) -> Observation | None:
    """Return a fresh observation; whether a re-observation happened is itself journaled."""
    max_age = ctx.config.limits.observation_max_age_seconds

    async def run() -> dict[str, Any]:
        live_check = ctx.needs_live_check
        ctx.needs_live_check = False
        if obs is not None and not live_check:
            captured = parse_iso(obs.captured_at)
            if captured and (utcnow() - captured).total_seconds() <= max_age * 0.8:
                return {"obs": obs.model_dump()}
        out = await ctx.act("browser.observe", {}, key=f"obs:{step}", attachment_id=attachment_id)
        live = out.obs
        if live_check and obs is not None and live is not None and live.url != obs.url and obs.url.startswith("http"):
            # the page was lost (e.g. worker restart): restore the last known page, then observe again
            ctx.note_limitation(f"browser state was lost and restored to {obs.url}; earlier page state may differ")
            nav = await ctx.act("browser.navigate", {"url": obs.url}, key=f"restore:{step}", attachment_id=attachment_id)
            if nav.ok and nav.observation:
                return {"obs": nav.observation, "at": now_iso(), "restored": True}
        return {"obs": out.observation, "at": now_iso()}

    data = await ctx.step(f"fresh:{step}", run)
    return Observation.model_validate(data["obs"]) if data.get("obs") else None


def _describe(out: ActionOutcome) -> str:
    if out.ok:
        v = out.verification or {}
        if v.get("checked"):
            return "done, expected result " + ("confirmed" if v.get("passed") else f"NOT confirmed {v.get('checks')}")
        return "done"
    if out.status.value == "DENIED":
        return f"not executed ({out.error_code or 'denied'}: {truncate(out.message or out.reason or '', 120)})"
    return f"{out.status.value.lower()} ({out.error_code}: {truncate(out.message or '', 120)})"
