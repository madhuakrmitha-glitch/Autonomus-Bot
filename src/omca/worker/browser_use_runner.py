"""Run one Browser Use agent task (executed by the worker in its own Browser Use virtualenv).

This file must not import ``omca``: Browser Use pins dependency versions that conflict with the
coordinator's, so it runs under a separate interpreter (``browser.agent.python``). The worker
passes a JSON request on stdin (secrets included, never on the command line) and reads one JSON
result line, prefixed with ``RESULT_PREFIX``, from stdout. Everything else goes to stderr.

Model calls go to the coordinator's OpenAI-compatible model gateway, so they use the configured
routes and count against the shared budget.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import traceback
from typing import Any

RESULT_PREFIX = "OMCA_BROWSER_USE_RESULT "


def _real(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


async def _final_state(session: Any, history: Any, max_text: int) -> dict[str, Any]:
    """The last real page the agent saw: its URL and screenshot from the step history, plus the live page text.

    The live tab can already be blank when the agent finishes, so the history is the primary source."""
    state: dict[str, Any] = {}
    urls = [u for u in history.urls() if _real(u)]
    if urls:
        state["url"] = urls[-1]
    shots = [s for s in history.screenshots() if s]
    if shots:
        state["screenshot_b64"] = shots[-1]
    try:
        live = await session.get_current_page_url()
        if _real(live) and (not state.get("url") or live == state["url"]):
            state["url"] = live
            state["title"] = await session.get_current_page_title()
            page = await session.get_current_page()
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            state["text"] = str(text or "")[:max_text]
            if "screenshot_b64" not in state:
                state["screenshot_b64"] = base64.b64encode(await session.take_screenshot()).decode("ascii")
    except Exception:  # noqa: BLE001 - the browser may already be gone
        pass
    return state


async def run(req: dict[str, Any]) -> dict[str, Any]:
    from browser_use import Agent, BrowserProfile, BrowserSession, ChatOpenAI

    llm = ChatOpenAI(model=req["model"], base_url=req["base_url"], api_key=req["api_key"],
                     max_completion_tokens=req.get("max_output_tokens", 4096), max_retries=2,
                     timeout=req.get("llm_timeout", 180))
    profile_kwargs: dict[str, Any] = {"headless": bool(req.get("headless", False)), "keep_alive": False,
                                      "user_data_dir": req.get("user_data_dir")}
    if req.get("channel") and req["channel"] != "chromium":
        profile_kwargs["channel"] = req["channel"]
    if req.get("downloads_path"):
        profile_kwargs["downloads_path"] = req["downloads_path"]
    session = BrowserSession(browser_profile=BrowserProfile(**profile_kwargs))
    task = req["task"]
    if req.get("start_url"):
        task = f"{task}\n\nStart at {req['start_url']}."
    agent = Agent(task=task, llm=llm, browser_session=session, use_vision=bool(req.get("use_vision", True)),
                  max_failures=int(req.get("max_failures", 3)),
                  max_actions_per_step=int(req.get("max_actions_per_step", 4)),
                  extend_system_message=req.get("extra_instructions") or None,
                  sensitive_data=req.get("sensitive_data") or None,
                  calculate_cost=False, generate_gif=False)
    final: dict[str, Any] = {}
    try:
        history = await asyncio.wait_for(agent.run(max_steps=int(req.get("max_steps", 40))),
                                         timeout=float(req.get("timeout_seconds", 900)))
        final = await _final_state(session, history, int(req.get("max_text_chars", 20000)))
    finally:
        try:
            await session.kill()
        except Exception:  # noqa: BLE001
            pass
    return {
        "done": history.is_done(),
        "success": history.is_successful(),
        "final_result": history.final_result(),
        "steps": history.number_of_steps(),
        "duration_seconds": history.total_duration_seconds(),
        "urls": [u for u in history.urls() if u][-20:],
        "errors": [e for e in history.errors() if e][-10:],
        "final": final,
    }


def main() -> int:
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    req = json.loads(sys.stdin.read())
    try:
        out = {"ok": True, **asyncio.run(run(req))}
    except TimeoutError:
        out = {"ok": False, "error": "TIMEOUT", "message": "the Browser Use run exceeded its time limit"}
    except Exception as exc:  # noqa: BLE001 - report every failure as JSON
        traceback.print_exc(file=sys.stderr)
        out = {"ok": False, "error": type(exc).__name__, "message": str(exc)[:1000]}
    sys.stdout.write(RESULT_PREFIX + json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
