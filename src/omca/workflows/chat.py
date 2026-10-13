"""Conversational workflow for general requests and follow-ups.

This workflow answers the user's question normally. A web chat is a conversation: every message
in it becomes a chat task, and the model sees only the earlier turns of that same conversation,
never the other chats. Messages that do not belong to a conversation (Telegram) keep the older
behavior of seeing a short digest of the most recent tasks.
"""

from __future__ import annotations

from typing import Any

from ..contracts import TaskStatus
from ..engine import WorkflowOutcome
from .common import system_prompt

CONVERSATION_TURNS = 30
REPLY_CHARS = 4000


def _digest(c: Any, current_id: str) -> str:
    text = ""
    for t in reversed(c.tasks.list(limit=5)):
        if t.id == current_id:
            continue
        text += f"\nPrevious task goal: {t.goal}\n"
        if t.terminal and t.result:
            summary = t.result.get("summary")
            if summary:
                text += f"Result summary: {summary}\n"
            limitations = t.result.get("limitations")
            if limitations:
                text += f"Limitations: {', '.join(limitations)}\n"
        elif t.status != TaskStatus.QUEUED:
            text += f"Status: {t.status.value}\n"
    return text.strip()


def _turns(c: Any, task: Any) -> list[dict[str, str]]:
    """Earlier turns of this task's conversation, oldest first, as user/assistant messages."""
    turns: list[dict[str, str]] = []
    prior = [t for t in reversed(c.tasks.list(limit=CONVERSATION_TURNS, conversation_id=task.conversation_id))
             if t.id != task.id and t.created_at <= task.created_at]
    for t in prior:
        turns.append({"role": "user", "content": t.goal})
        if t.terminal and t.result and t.result.get("summary"):
            turns.append({"role": "assistant", "content": t.result["summary"][:REPLY_CHARS]})
        elif not t.terminal and t.status != TaskStatus.QUEUED:
            turns.append({"role": "assistant", "content": f"(still working on this: {t.status.value.lower().replace('_', ' ')})"})
    return turns


async def chat_workflow(ctx: Any) -> WorkflowOutcome:
    c = ctx.c
    goal = ctx.task.goal

    ctx.progress("understanding the request", phase="plan")

    sys_msg = system_prompt(ctx, "You are a helpful assistant.")
    if ctx.task.conversation_id:
        history = [{"role": "system", "content": sys_msg}, *_turns(c, ctx.task), {"role": "user", "content": goal}]
    else:
        digest = _digest(c, ctx.task.id)
        if digest:
            sys_msg += f"\n\nRecent conversation and task context:\n{digest}"
        history = [{"role": "system", "content": sys_msg}, {"role": "user", "content": goal}]

    for ev in c.tasks.events(ctx.task.id):
        if ev["kind"] in ("instruction", "answer"):
            history.append({"role": "user", "content": ev.get("text", "")})

    response = await ctx.model("planning", history, key="chat_response", max_output_tokens=2048)

    await ctx.act("telegram.send_owner", {"text": response.text}, key="deliver")

    return WorkflowOutcome("COMPLETED", "Responded to the user.", result={"summary": response.text[:REPLY_CHARS]})
