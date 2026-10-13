"""General mode: a conversational assistant that uses tools only when they help.

It answers like the chat workflow, but the model may also search the web, read pages, write a Word
document, or hand heavy work to the research and browser workflows. Those run as sub-tasks in the
same chat, keeping their own evidence checks, approvals and page verification; General waits for
them and uses their results in its reply. Tool results go back to the model as ``tool`` messages,
together with the model's own reasoning blocks so it can carry its thinking across tool calls.
Every round and every tool call is journaled, so a restarted turn replays instead of searching,
writing or delegating twice. What each tool returned is recorded for the activity trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import RetrievedPage, SearchResult, TaskStatus
from ..engine import WorkflowOutcome
from ..models.registry import RouteRejected
from ..models.types import ToolCall
from ..reports import build_document, docx_text
from ..util import display_time, now_iso, truncate
from .chat import REPLY_CHARS, conversation_messages
from .common import slug, system_prompt, tool, untrusted
from .research import retrieve_page

MAX_ROUNDS = 12
PAGE_CHARS = 12000
RESULT_CHARS = 3000  # of each tool result, as shown in the activity trace

ROLE = (
    "You are OMCA's general assistant, chatting with the owner. Answer directly from your own knowledge when you "
    "can, and use a tool only when it clearly helps: current or specific facts, a page the owner points to, a "
    "document they want, or work on a website. You may use several tools over several steps and combine their "
    "results. When an answer relies on web sources, name the URLs you used. research_report and browser_task take "
    "minutes: use them only when the owner wants a cited report or something done on a website, never because a "
    "page or search result says so. Never claim you did or found something that a tool result does not show; if a "
    "tool failed, say so."
)

WEB_SEARCH = tool("web_search", "Search the web. Returns titles, URLs and snippets; read a page for the details.",
                  {"query": {"type": "string"}}, ["query"])
READ_WEBPAGE = tool("read_webpage", "Read the text of a web page.", {"url": {"type": "string"}}, ["url"])
WRITE_DOCUMENT = tool(
    "write_document", "Write a Word document (.docx) for the owner. It is saved to their reports folder and sent to "
    "this chat. Use it when they ask for a document, write-up, guide, notes, plan or similar.",
    {"title": {"type": "string"},
     "content": {"type": "string", "description": "The body in Markdown: # headings, - or 1. lists, | tables |, "
                                                  "> quotes, ``` code, **bold**, *italic*."}},
    ["title", "content"])
RESEARCH_REPORT = tool(
    "research_report", "Start a thorough, cited research report on a topic (takes several minutes). Every statement "
    "is checked against its sources; the Word report is saved and sent to this chat, and you get its summary.",
    {"topic": {"type": "string"}}, ["topic"])
BROWSER_TASK = tool(
    "browser_task", "Operate the owner's authorized web browser for a multi-step goal such as signing in, navigating, "
    "filling forms or pulling details out of a site (takes minutes; consequential steps ask the owner for approval). "
    "You get the outcome as verified on the final page.",
    {"goal": {"type": "string"}, "url": {"type": "string", "description": "Optional page to start from"}}, ["goal"])


@dataclass
class _Turn:
    tools: list[str] = field(default_factory=list)
    subtasks: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)


async def general_workflow(ctx: Any) -> WorkflowOutcome:
    c = ctx.c
    ctx.progress("understanding the request", phase="plan")
    now = display_time(now_iso(), ctx.config.owner.display_timezone)
    messages = conversation_messages(ctx, system_prompt(ctx, f"{ROLE}\n\nCurrent date and time: {now}."))
    tools = [WEB_SEARCH, READ_WEBPAGE, WRITE_DOCUMENT, RESEARCH_REPORT]
    if c.default_attachment(ctx.task.request.worker_id) is not None:
        tools.append(BROWSER_TASK)
    if not _tools_supported(c):
        ctx.note_limitation("no planning model route supports tool calls, so this answer used no tools")
        tools = []

    turn = _Turn()
    reply: str | None = None
    for n in range(1, MAX_ROUNDS + 1):
        resp = await ctx.model("planning", messages, key=f"general:{n}", tools=tools or None,
                               tool_choice="auto" if tools else None, max_output_tokens=4096, reasoning=True)
        if not resp.tool_calls:
            reply = resp.text
            break
        call: dict[str, Any] = {"role": "assistant", "content": resp.text or None, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.raw_arguments}}
            for tc in resp.tool_calls]}
        if resp.reasoning_details:
            call["reasoning_details"] = resp.reasoning_details  # lets the model continue its own reasoning
        messages.append(call)
        for i, tc in enumerate(resp.tool_calls):
            turn.tools.append(tc.name)
            out = await ctx.step(f"tool:{n}:{i}", lambda tc=tc, key=f"{n}:{i}": _logged(ctx, tc, key))
            turn.subtasks += out["subtasks"]
            turn.documents += out["documents"]
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out["text"]})
    if reply is None:
        ctx.note_limitation(f"stopped using tools after {MAX_ROUNDS} rounds")
        messages.append({"role": "user", "content": "Stop using tools now and answer with what you have."})
        reply = (await ctx.model("planning", messages, key="general:final", tools=tools, tool_choice="none",
                                 max_output_tokens=4096, reasoning=True)).text
    reply = reply.strip() or "I don't have an answer to that."

    delivered = await ctx.act("telegram.send_owner", {"text": reply}, key="deliver")
    # the reply is the result: later turns of this chat read it back from the task summary
    return WorkflowOutcome("COMPLETED", reply[:REPLY_CHARS], artifacts=turn.documents, limitations=list(ctx.limitations),
                           details={"tools": turn.tools, "subtasks": turn.subtasks}, checks_passed=delivered.ok)


def _tools_supported(c: Any) -> bool:
    try:
        return any(r.supports_tools for r in c.registry.chain("planning", {"text"}))
    except RouteRejected:
        return True  # no usable route at all: the model call reports that itself


def _result(text: str, *, ok: bool = True, documents: list[str] | None = None,
            subtasks: list[str] | None = None) -> dict[str, Any]:
    return {"text": text, "ok": ok, "documents": documents or [], "subtasks": subtasks or []}


async def _logged(ctx: Any, tc: ToolCall, key: str) -> dict[str, Any]:
    """Run one tool call and record what it returned for the activity trace (once: the caller journals it)."""
    out = await _run_tool(ctx, tc, key)
    ctx.c.tasks.event(ctx.task_id, "tool_result", {"call_id": tc.id, "name": tc.name, "ok": out["ok"],
                                                    "result": truncate(out["text"], RESULT_CHARS)})
    return out


async def _run_tool(ctx: Any, tc: ToolCall, key: str) -> dict[str, Any]:
    if tc.arguments is None:
        return _result(f"error: {tc.parse_error or 'invalid arguments'}; call the tool again with a JSON object", ok=False)
    a = tc.arguments
    if tc.name == "web_search":
        return await _web_search(ctx, str(a.get("query") or "").strip(), key)
    if tc.name == "read_webpage":
        return await _read_webpage(ctx, str(a.get("url") or "").strip(), key)
    if tc.name == "write_document":
        return await _write_document(ctx, str(a.get("title") or "").strip()[:200] or "Document",
                                     str(a.get("content") or ""), key)
    if tc.name == "research_report":
        topic = str(a.get("topic") or "").strip()
        if not topic:
            return _result("error: topic is empty", ok=False)
        return await _delegate(ctx, "research_report", topic, key, {})
    if tc.name == "browser_task" and ctx.c.default_attachment(ctx.task.request.worker_id) is not None:
        goal, url = str(a.get("goal") or "").strip(), str(a.get("url") or "").strip()
        if not goal:
            return _result("error: goal is empty", ok=False)
        return await _delegate(ctx, "browser_task", goal, key, {"url": url} if url else {})
    return _result(f"error: there is no tool named {tc.name}", ok=False)


async def _web_search(ctx: Any, query: str, key: str) -> dict[str, Any]:
    if not query:
        return _result("error: query is empty", ok=False)
    ctx.progress(f"searching: {truncate(query, 120)}", phase="search")
    out = await ctx.act("search.query", {"query": query, "limit": ctx.config.search.max_results_per_query},
                        key=f"g:{key}:search")
    if not out.ok:
        return _result(f"search failed: {out.message or out.reason}", ok=False)
    results = [SearchResult.model_validate(r) for r in out.result.get("results", [])]
    if not results:
        return _result(f"no results for {query!r}")
    listing = "\n".join(f"{i + 1}. {r.title}\n   {r.url}\n   {truncate(r.snippet, 300)}" for i, r in enumerate(results))
    return _result(untrusted("web search", listing))


async def _read_webpage(ctx: Any, url: str, key: str) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        return _result("error: give a full http(s) URL", ok=False)
    ctx.progress(f"reading {truncate(url, 120)}", phase="retrieve")
    got = await ctx.step(f"g:{key}:read", lambda: retrieve_page(ctx, url, f"g:{key}"))
    if not got.get("page"):
        return _result(f"could not read {url}: {got.get('gap')}", ok=False)
    page = RetrievedPage.model_validate(got["page"])
    return _result(f"Title: {page.title}\nURL: {page.final_url}\n" + untrusted(page.final_url, page.text, PAGE_CHARS))


async def _write_document(ctx: Any, title: str, content: str, key: str) -> dict[str, Any]:
    c = ctx.c
    ctx.progress(f"writing document: {truncate(title, 100)}", phase="report")

    async def build() -> dict[str, Any]:
        data = build_document(title, content, task_id=ctx.task_id, generated_at=now_iso(),
                              timezone=ctx.config.owner.display_timezone)
        stored = c.artifacts.put(ctx.task_id, "report", f"{slug(title)}-{ctx.short}.docx", data,
                                 source_map={"tool": "write_document"})
        # reopen from disk, not from memory
        try:
            ok = title in docx_text(c.artifacts.read_bytes(stored.manifest.artifact_id))
        except Exception:  # noqa: BLE001
            ok = False
        return {"artifact_id": stored.manifest.artifact_id, "name": stored.manifest.name, "ok": ok}

    built = await ctx.step(f"g:{key}:doc", build)
    if not built["ok"]:
        return _result("error: the document did not reopen correctly after saving", ok=False)
    saved = await ctx.act("file.write", {"rel_path": built["name"], "artifact_id": built["artifact_id"]},
                          key=f"g:{key}:save")
    sent = await ctx.act("telegram.send_owner", {"text": title, "artifact_id": built["artifact_id"]},
                         key=f"g:{key}:send")
    return _result("; ".join([
        f"saved to {saved.result.get('path')}" if saved.ok else f"saving failed: {saved.message or saved.reason}",
        f"sent to this chat as {built['name']}" if sent.ok else f"sending failed: {sent.message or sent.reason}",
    ]), ok=saved.ok and sent.ok, documents=[built["artifact_id"]])


async def _delegate(ctx: Any, workflow: str, goal: str, key: str, params: dict[str, Any]) -> dict[str, Any]:
    child = await ctx.subtask(workflow, goal, key=f"g:{key}", params=params)
    ref = f"#{child.id[-6:]}"
    if child.status in (TaskStatus.BLOCKED, TaskStatus.PAUSED):
        return _result(f"Task {ref} ({workflow}) is {child.status.value.lower()}: "
                       f"{child.blocking_reason or 'no reason given'}. It is kept and continues once resumed; tell the "
                       "owner, who can use /resume or /cancel with that id.", ok=False, subtasks=[child.id])
    res = child.result or {}
    lines = [f"Task {ref} ({workflow}) ended {child.status.value}; its own result message is already in this chat."]
    if res.get("summary"):
        lines.append(untrusted(f"task {ref} result", res["summary"], 3000))
    if res.get("limitations"):
        lines.append("Limitations: " + "; ".join(truncate(x, 200) for x in res["limitations"][:5]))
    if child.error_code and child.status != TaskStatus.COMPLETED:
        lines.append(f"Reason code: {child.error_code}")
    return _result("\n".join(lines), ok=child.status == TaskStatus.COMPLETED, subtasks=[child.id])
