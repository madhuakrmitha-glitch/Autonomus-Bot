"""Research -> evidence -> cited Word report -> saved file -> Telegram delivery (the first
complete workflow; FR03, FR05, FR06, FR09, FR10, FR16).

Every external step (search, retrieval, browser fallback, image fetch) is an allowlisted
operation through ``ctx.act``; every model step is metered through ``ctx.model``. Quotes
are verified against the captured source text before they become evidence. A failed API
fetch falls back to the authorized browser; when both fail the gap is reported, not hidden.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..contracts import EvidenceRecord, Rect, RetrievedPage, SearchResult
from ..engine import TaskBlocked, WorkflowOutcome
from ..evidence import locate_excerpt, numbers_in
from ..models.types import ImageInput
from ..util import truncate
from .common import (
    finalize_report,
    looks_like_injection,
    model_json,
    model_review,
    review_claims,
    system_prompt,
    untrusted,
)

VISUAL_HINT = re.compile(r"\b(chart|graph|plot|figure|diagram|image|photo|infographic|map|visual|screenshot|table)s?\b", re.I)
SKIP_IMAGE = re.compile(r"(logo|icon|sprite|avatar|badge|pixel|tracking|spacer|emoji|favicon)", re.I)


async def research_report(ctx) -> WorkflowOutcome:
    c, cfg = ctx.c, ctx.config
    req = ctx.task.request
    goal = ctx.task.goal

    # 1. plan --------------------------------------------------------------------------------------------
    ctx.progress("planning the research", phase="plan")
    plan = await model_json(ctx, "planning", [
        {"role": "system", "content": system_prompt(ctx, "You plan web research for a cited report.")},
        {"role": "user", "content": (
            f"Research request: {goal}\n\nReturn JSON {{\"title\": str, \"queries\": [up to "
            f"{cfg.search.max_queries_per_task} focused web search queries], \"questions\": [key questions the report "
            "must answer]}. Prefer queries that surface primary sources.")},
    ], key="plan", default={})
    plan = plan if isinstance(plan, dict) else {}
    queries = [q for q in (plan.get("queries") or []) if isinstance(q, str) and q.strip()][: cfg.search.max_queries_per_task]
    if not queries:
        queries = [goal[:300]]
    title = str(plan.get("title") or truncate(goal, 90))
    questions = [q for q in (plan.get("questions") or []) if isinstance(q, str)][:8]

    # 2. search (policy-mediated, provider-independent) ---------------------------------------------------
    results: list[SearchResult] = []
    search_provider = None
    if req.source_preference != "browser":
        for i, q in enumerate(queries):
            ctx.progress(f"searching: {q}", phase="search")
            out = await ctx.act("search.query", {"query": q, "limit": cfg.search.max_results_per_query}, key=f"search:{i}")
            if out.ok:
                search_provider = out.result.get("provider")
                results.extend(SearchResult.model_validate(r) for r in out.result.get("results", []))
            else:
                ctx.note_limitation(f"search for '{truncate(q, 80)}' failed: {out.message}")
    urls: list[str] = []
    for s in req.sources:
        if s.startswith(("http://", "https://")) and s not in urls:
            urls.append(s)
    for r in c.search.rank(results):
        if r.url not in urls:
            urls.append(r.url)
    urls = urls[: cfg.search.max_pages_per_task]
    if not urls:
        return WorkflowOutcome("FAILED", "No sources were found for this request.", limitations=ctx.limitations,
                               error_code="EVIDENCE_GAP")

    # 3. retrieve with browser fallback --------------------------------------------------------------------
    pages: list[RetrievedPage] = []
    for i, url in enumerate(urls):
        ctx.progress(f"reading source {i + 1}/{len(urls)}: {url}", phase="retrieve")
        page = await ctx.step(f"retrieve:{i}", lambda url=url, i=i: retrieve_page(ctx, url, i))
        if page.get("page"):
            pages.append(RetrievedPage.model_validate(page["page"]))
        else:
            ctx.note_limitation(f"evidence gap - {url}: {page.get('gap')}")
    if not pages:
        return WorkflowOutcome("FAILED", "None of the sources could be read through the API or the authorized browser.",
                               limitations=ctx.limitations, error_code="EVIDENCE_GAP")

    # 4. extract verified quotes as evidence ----------------------------------------------------------------
    findings: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        ctx.progress(f"extracting evidence from {page.final_url}", phase="extract")
        got = await ctx.step(f"evidence:{i}", lambda page=page, i=i: _extract(ctx, page, i, goal, questions))
        findings.extend(got["findings"])
        if got["dropped"]:
            ctx.note_limitation(f"{got['dropped']} quote(s) from {urlsplit(page.final_url).hostname} could not be found "
                                "verbatim in the source and were discarded")

    # 5. visual evidence (actual pixels to an open vision model) ----------------------------------------------
    figures: list[tuple[bytes, str, list[str]]] = []
    if VISUAL_HINT.search(goal) or req.params.get("analyse_images"):
        visual = await ctx.step("visual", lambda: _visual(ctx, pages, goal))
        findings.extend(visual["findings"])
        for f in visual["figures"]:
            figures.append((c.artifacts.read_bytes(f["artifact_id"]), f["caption"], [f["evidence_id"]]))
        for lim in visual["limitations"]:
            ctx.note_limitation(lim)

    if not findings:
        return WorkflowOutcome("FAILED", "Sources were read but no verifiable evidence relevant to the request was found.",
                               limitations=ctx.limitations, error_code="EVIDENCE_GAP")

    # 6. synthesise -------------------------------------------------------------------------------------------
    ctx.progress("synthesising the report", phase="synthesise")
    evidence = {e.evidence_id: e for e in c.evidence.for_task(ctx.task_id)}
    listing = "\n".join(f"[{f['evidence_id']}] ({f['source']}) {f['statement']} | quote: {truncate(f.get('quote') or '', 300)}"
                        for f in findings)
    synth = await model_json(ctx, "summarisation", [
        {"role": "system", "content": system_prompt(ctx, "You write concise, well-sourced research reports. Use only the "
                                                         "findings provided; every statement must cite evidence ids from "
                                                         "the list. Report disagreement between sources instead of picking "
                                                         "one. Do not invent numbers.")},
        {"role": "user", "content": (
            f"Request: {goal}\nKey questions: {questions}\n\nFindings (evidence id, source, statement, quote):\n"
            + untrusted("findings", listing, 30000)
            + "\n\nReturn JSON {\"title\": str, \"summary\": [{\"text\": str, \"evidence_ids\": [ids]}], "
              "\"sections\": [{\"heading\": str, \"statements\": [{\"text\": str, \"evidence_ids\": [ids]}]}], "
              "\"disagreements\": [{\"text\": str, \"evidence_ids\": [ids]}], \"open_questions\": [str]}")},
    ], key="synthesise", max_output_tokens=4096, default={})
    synth = synth if isinstance(synth, dict) else {}
    title = str(synth.get("title") or title)

    # 7. review: deterministic evidence checks, then open-model entailment check -------------------------------
    ctx.progress("checking statements against their evidence", phase="review")
    summary = review_claims(list(synth.get("summary") or []), evidence)
    sections: list[tuple[str, list[dict[str, Any]]]] = []
    for s in synth.get("sections") or []:
        if isinstance(s, dict) and s.get("statements"):
            sections.append((str(s.get("heading") or "Findings"), review_claims(list(s["statements"]), evidence)))
    disagreements = review_claims([d for d in synth.get("disagreements") or [] if isinstance(d, dict)], evidence)
    if disagreements:
        sections.append(("Where sources disagree", disagreements))
    if not summary and not sections:
        summary = review_claims([{"text": f["statement"], "evidence_ids": [f["evidence_id"]]} for f in findings[:12]], evidence)
    all_stmts = summary + [x for _, st in sections for x in st]
    await model_review(ctx, all_stmts, evidence)
    if figures:
        sections.append(("Figures", []))
    figure_map = {"Figures": figures} if figures else {}
    for q in synth.get("open_questions") or []:
        if isinstance(q, str):
            ctx.note_limitation(f"open question: {truncate(q, 200)}")
    unsupported = sum(1 for s in all_stmts if s["support"] != "supported")
    if unsupported:
        ctx.note_limitation(f"{unsupported} statement(s) are marked unsupported, uncertain or disputed in the report")

    # 8. report, save, validate, deliver ---------------------------------------------------------------------
    method = {
        "Search provider": search_provider or "none (owner-supplied sources)",
        "Queries": "; ".join(queries),
        "Sources read": f"{len(pages)} of {len(urls)} selected",
        "Model routes": ", ".join(sorted({r.id for r in [*_routes(c, 'planning'), *_routes(c, 'summarisation')]})) or "-",
        "Evidence rule": "quotes verified verbatim against captured text; numbers checked against cited evidence",
    }
    built, saved, delivered, checks = await finalize_report(
        ctx, title=title, summary=summary, sections=sections, figures=figure_map, disagreements=[],
        limitations=list(ctx.limitations), method=method, deliver_caption=f"Report: {title}")
    ok = all(checks.values())
    if not checks["delivered"]:
        ctx.note_limitation("the report is saved but Telegram delivery is not confirmed yet")
    summary_text = " ".join(s["text"] for s in summary[:3]) or f"Report '{title}' prepared."
    return WorkflowOutcome(
        status="COMPLETED" if ok else "PARTIAL",
        summary=f"{title}\n{truncate(summary_text, 900)}\nSaved to: {saved.result.get('path', '-')}",
        artifacts=[built["artifact_id"]], limitations=list(ctx.limitations),
        details={"checks": checks, "validation": built["validation"], "sources": len(pages),
                 "evidence": len(evidence), "statements": len(all_stmts)},
        checks_passed=ok,
    )


def _routes(c, role: str):
    try:
        return c.registry.chain(role, {"text"})[:1]
    except Exception:  # noqa: BLE001
        return []


async def retrieve_page(ctx, url: str, key: str | int) -> dict[str, Any]:
    """Read a page through the retrieval API, falling back to the authorized browser.

    Returns {"page": RetrievedPage dump} or {"gap": reason}; ``key`` keeps the actions of each read distinct.
    """
    c = ctx.c
    pref = ctx.task.request.source_preference
    api_error = None
    if pref != "browser":
        out = await ctx.act("web.retrieve", {"url": url, "formats": ["markdown"]}, key=f"fetch:{key}")
        if out.ok:
            return {"page": out.result["page"]}
        api_error = out.message or out.reason
    att = c.default_attachment(ctx.task.request.worker_id)
    if att is None:
        return {"gap": f"API retrieval failed ({api_error}); no authorized browser session is configured"}
    try:
        nav = await ctx.act("browser.navigate", {"url": url}, key=f"browse:{key}", attachment_id=att.id)
    except TaskBlocked as exc:
        return {"gap": f"API retrieval failed ({api_error}); browser unavailable ({exc})"}
    if not nav.ok:
        return {"gap": f"API retrieval failed ({api_error}); browser could not open it ({nav.message or nav.reason})"}
    obs = nav.obs
    if obs is None or not obs.text.strip():
        return {"gap": "the page rendered no readable text in the browser"}
    if any(e.input_type == "password" for e in obs.elements):
        return {"gap": "the page asks for a login; sign in on the worker browser and steer the task to retry"}
    from ..util import sha256_text

    page = RetrievedPage(provider=f"browser:{att.id}", source_url=url, final_url=obs.url, status=200,
                         captured_at=obs.captured_at, title=obs.title, text=obs.text,
                         content_sha256=sha256_text(obs.text), screenshot_artifact_id=obs.screenshot_artifact_id,
                         image_urls=[im.src for im in obs.images if im.src])
    return {"page": page.model_dump()}


async def _extract(ctx, page: RetrievedPage, i: int, goal: str, questions: list[str]) -> dict[str, Any]:
    c = ctx.c
    text = page.text
    chunks = [text[k: k + 14000] for k in range(0, min(len(text), 42000), 14000)] or [""]
    findings: list[dict[str, Any]] = []
    dropped = 0
    stale, stale_note = c.memory.is_stale(page.final_url, page.source_date)
    for ci, chunk in enumerate(chunks):
        data = await model_json(ctx, "summarisation", [
            {"role": "system", "content": system_prompt(ctx, "You extract evidence for a research report.")},
            {"role": "user", "content": (
                f"Request: {goal}\nKey questions: {questions}\nSource: {page.final_url} (title: {page.title})\n\n"
                + untrusted(page.final_url, chunk, 15000)
                + "\n\nReturn JSON {\"findings\": [{\"statement\": str, \"quote\": str}]} with up to 6 findings "
                  "relevant to the request. Each quote must be copied exactly from the source (one or two sentences). "
                  "Return an empty list if nothing is relevant.")},
        ], key=f"extract:{i}:{ci}", max_output_tokens=1500, default={"findings": []})
        for j, f in enumerate((data or {}).get("findings") or []):
            if not isinstance(f, dict):
                continue
            quote, statement = str(f.get("quote") or ""), str(f.get("statement") or "")
            if looks_like_injection(quote) or looks_like_injection(statement):
                ctx.note_limitation(f"possible prompt-injection text on {urlsplit(page.final_url).hostname} was "
                                    "ignored and not acted on")
            loc = locate_excerpt(text, quote)
            if not loc or not statement:
                dropped += 1
                continue
            ev = c.evidence.record(
                ctx.task_id, kind="text_excerpt", source_url=page.source_url, final_url=page.final_url,
                source_date=page.source_date, captured_at=page.captured_at, excerpt=quote,
                locator={"normalized_offsets": list(loc), "text_fragment": quote[:120], "chunk": ci},
                content_for_hash=text, provider=page.provider, limitations=stale_note if stale else None,
                stable_key=f"extract:{i}:{ci}:{j}",
            )
            findings.append({"evidence_id": ev.evidence_id, "statement": statement, "quote": ev.excerpt,
                             "source": urlsplit(page.final_url).hostname})
    return {"findings": findings, "dropped": dropped}


async def _visual(ctx, pages: list[RetrievedPage], goal: str) -> dict[str, Any]:
    """Send real image pixels to the vision route; confirm read values with a zoomed re-read."""
    c = ctx.c
    candidates: list[tuple[str, str]] = []
    for p in pages:
        for src in p.image_urls:
            if SKIP_IMAGE.search(src) or src in [x[0] for x in candidates]:
                continue
            candidates.append((src, p.final_url))
    candidates = candidates[:3]
    findings, figures, limitations = [], [], []
    att = c.default_attachment(ctx.task.request.worker_id)
    for k, (src, page_url) in enumerate(candidates):
        out = await ctx.act("web.fetch_image", {"src": src, "name": f"figure_{k}"}, key=f"image:{k}")
        if not out.ok and att is not None:
            try:
                out = await ctx.act("browser.fetch_image", {"src": src, "name": f"figure_{k}"}, key=f"image_b:{k}",
                                    attachment_id=att.id)
            except TaskBlocked:
                pass
        if not out.ok:
            limitations.append(f"image {truncate(src, 80)} could not be retrieved ({out.message})")
            continue
        art_id = out.result["artifact_id"]
        manifest = c.artifacts.get(art_id)
        data = c.artifacts.read_bytes(art_id)
        analysis = await model_json(ctx, "vision", [
            {"role": "system", "content": system_prompt(ctx, "You analyse images for a research report. You see the "
                                                             "actual image pixels.")},
            {"role": "user", "content": (
                f"Request: {goal}\nImage source: {src} (on page {page_url}).\nDescribe what the image shows that is "
                "relevant. If it is a chart or table, give axes, legend, series and the relationships it shows. For "
                "each value you read report the label, the value exactly as printed, and legibility 'clear' or "
                "'unclear'. Never estimate an unreadable value. Return JSON {\"relevant\": bool, \"kind\": str, "
                "\"description\": str, \"relationships\": [str], \"values\": [{\"label\": str, \"value\": str, "
                "\"legibility\": \"clear|unclear\", \"bbox\": [x0,y0,x1,y1]}]} with bbox on a 0-1000 scale.")},
        ], key=f"vision:{k}", images=[ImageInput(data, manifest.media_type, art_id)], default={"relevant": False})
        if not isinstance(analysis, dict) or not analysis.get("relevant"):
            continue
        confirmed, unclear = [], []
        for vi, v in enumerate((analysis.get("values") or [])[:12]):
            if not isinstance(v, dict):
                continue
            label, value = str(v.get("label") or ""), str(v.get("value") or "")
            if v.get("legibility") != "clear" or not numbers_in(value):
                unclear.append(label or value)
                continue
            ok = await _confirm_value(ctx, art_id, manifest, v, k, vi)
            (confirmed if ok else unclear).append(f"{label}: {value}" if ok else label or value)
        lim = None
        if unclear:
            lim = f"values not confirmed (unreadable or inconsistent on zoom): {', '.join(unclear[:8])}"
            limitations.append(f"figure {k + 1}: {lim}")
        ev = c.evidence.record(
            ctx.task_id, kind="image_region", source_url=src, final_url=src,
            captured_at=((out.receipt or {}).get("finished_at") or manifest.created_at),
            content_for_hash=manifest.sha256, image_artifact_id=art_id,
            region=Rect(x=0, y=0, width=manifest.width or 0, height=manifest.height or 0),
            locator={"page_url": page_url, "confirmed_values": confirmed},
            limitations=("read by an open vision model; " + (lim or "all reported values confirmed on zoom")),
            provider="vision", stable_key=f"visual:{k}",
        )
        rel = "; ".join(str(r) for r in (analysis.get("relationships") or [])[:4])
        statement = f"{analysis.get('description') or 'Figure'}{(' Relationships: ' + rel) if rel else ''}"
        if confirmed:
            statement += f" Confirmed values: {', '.join(confirmed)}."
        findings.append({"evidence_id": ev.evidence_id, "statement": truncate(statement, 600), "quote": "",
                         "source": urlsplit(src).hostname})
        figures.append({"artifact_id": art_id, "caption": f"Figure {k + 1}: {truncate(str(analysis.get('description') or ''), 160)}",
                        "evidence_id": ev.evidence_id})
    return {"findings": findings, "figures": figures, "limitations": limitations}


async def _confirm_value(ctx, art_id: str, manifest, v: dict[str, Any], k: int, vi: int) -> bool:
    """Crop the reported region at higher resolution and re-read it; disagreement means 'unconfirmed'."""
    bbox = v.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4 and manifest.width and manifest.height):
        return False
    try:
        x0, y0, x1, y1 = (float(b) for b in bbox)
    except (TypeError, ValueError):
        return False
    pad = 0.04
    rect = Rect(x=max(0.0, (x0 / 1000 - pad) * manifest.width), y=max(0.0, (y0 / 1000 - pad) * manifest.height),
                width=((x1 - x0) / 1000 + 2 * pad) * manifest.width, height=((y1 - y0) / 1000 + 2 * pad) * manifest.height)
    try:
        crop = ctx.c.artifacts.crop(art_id, rect, name=f"figure_{k}_value_{vi}.png", upscale_to=768)
    except Exception:  # noqa: BLE001
        return False
    data = ctx.c.artifacts.read_bytes(crop.manifest.artifact_id)
    ans = await model_json(ctx, "vision", [
        {"role": "user", "content": "This is a zoomed crop of a chart or table. What value text is printed here? If it "
                                    "cannot be read with confidence answer UNREADABLE. Return JSON {\"text\": str}."},
    ], key=f"zoom:{k}:{vi}", images=[ImageInput(data, "image/png", crop.manifest.artifact_id)], default={"text": "UNREADABLE"})
    text = str((ans or {}).get("text") or "")
    if "UNREADABLE" in text.upper():
        return False
    return bool(numbers_in(text) & numbers_in(str(v.get("value"))))


def evidence_by_id(c, task_id: str) -> dict[str, EvidenceRecord]:
    return {e.evidence_id: e for e in c.evidence.for_task(task_id)}
