"""Conversational workflow for general requests and follow-ups.

This workflow answers the user's question normally, keeping previous
conversations or the session's context in mind, rather than attempting
to automate a research or browser task.
"""

from __future__ import annotations

from typing import Any

from ..contracts import TaskStatus
from ..engine import WorkflowOutcome
from .common import system_prompt


async def chat_workflow(ctx: Any) -> WorkflowOutcome:
    c = ctx.c
    goal = ctx.task.goal

    ctx.progress("understanding the request", phase="plan")

    recent_tasks = c.tasks.list(limit=5)
    context_text = ""
    for t in reversed(recent_tasks):
        if t.id == ctx.task.id:
            continue
        context_text += f"\nPrevious task goal: {t.goal}\n"
        if t.terminal and t.result:
            summary = t.result.get("summary")
            if summary:
                context_text += f"Result summary: {summary}\n"
            limitations = t.result.get("limitations")
            if limitations:
                context_text += f"Limitations: {', '.join(limitations)}\n"
        elif t.status != TaskStatus.QUEUED:
            context_text += f"Status: {t.status.value}\n"

    sys_msg = system_prompt(ctx, "You are a helpful assistant.")
    if context_text.strip():
        sys_msg += f"\n\nRecent conversation and task context:\n{context_text.strip()}"

    history = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": goal}
    ]

    for ev in c.tasks.events(ctx.task.id):
        if ev["kind"] == "instruction":
            history.append({"role": "user", "content": ev.get("text", "")})
        elif ev["kind"] == "answer":
            history.append({"role": "user", "content": ev.get("text", "")})

    response = await ctx.model("planning", history, key="chat_response", max_output_tokens=2048)

    await ctx.act("telegram.send_owner", {"text": response.text}, key="deliver")

    return WorkflowOutcome("COMPLETED", "Responded to the user.", result={"summary": response.text[:500]})
