"""``browser.agent_task``: run the Browser Use agent in the authorized browser profile.

Browser Use runs as a subprocess under its own interpreter (see ``browser_use_runner.py``). Before
it starts, this worker's Playwright session for the attachment is closed so the agent can open the
same Chrome profile (a profile can only be open in one browser at a time).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import BrowserAgentConfig, BrowserCredential
from ..contracts import ErrorCode, WorkerJob
from ..secrets import resolve
from .agent import HandlerResult, JobContext, JobFailure
from .browser_use_runner import RESULT_PREFIX

RUNNER = Path(__file__).with_name("browser_use_runner.py")


@dataclass
class BrowserAgentSettings:
    config: BrowserAgentConfig
    gateway_url: str
    gateway_token: str
    max_text_chars: int = 20000


def _parse(stdout: bytes) -> dict[str, Any] | None:
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    return None


def _local_path(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _installed(python: str) -> bool:
    return os.path.exists(python)


def _sensitive_data(credentials: list[BrowserCredential]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for c in credentials:
        for part, ref in (("username", c.username_ref), ("password", c.password_ref)):
            value = resolve(ref)
            if value:
                out.setdefault(c.origin, {})[f"{c.id}_{part}"] = value
    return out


def make_browser_agent_handlers(manager: Any, settings: BrowserAgentSettings,
                                credentials: list[BrowserCredential] | None = None) -> dict[str, Any]:
    cfg = settings.config
    credentials = credentials or []

    async def agent_task(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        att = manager.attachments.get(job.attachment_id) if job.attachment_id else None
        if att is None:
            raise JobFailure(ErrorCode.AUTH_REQUIRED, f"browser attachment {job.attachment_id} is not configured on this worker")
        if not att.authorized:
            raise JobFailure(ErrorCode.AUTH_REQUIRED, f"browser attachment {att.id} is not authorized by the owner")
        if att.mode != "persistent_profile" or not att.user_data_dir:
            raise JobFailure(ErrorCode.UNSUPPORTED_CAPABILITY, "the Browser Use engine needs a persistent_profile attachment")
        if not _installed(cfg.python):
            raise JobFailure(ErrorCode.WORKER_UNAVAILABLE, f"Browser Use interpreter not found: {cfg.python}")
        await manager.release_session(att.id)

        a = job.arguments
        downloads = os.path.join(ctx.workspace, "downloads", job.task_id)
        req = {
            "task": a["goal"], "start_url": a.get("start_url"), "max_steps": a.get("max_steps") or cfg.max_steps,
            "model": cfg.model, "base_url": settings.gateway_url, "api_key": settings.gateway_token,
            "max_output_tokens": cfg.max_output_tokens, "use_vision": cfg.use_vision,
            "max_actions_per_step": cfg.max_actions_per_step, "extra_instructions": cfg.extra_instructions,
            "user_data_dir": _local_path(att.user_data_dir),
            "channel": att.channel, "headless": att.headless, "timeout_seconds": cfg.max_minutes * 60,
            "max_text_chars": settings.max_text_chars, "downloads_path": downloads,
        }
        sensitive = _sensitive_data(credentials)
        if sensitive:
            # origin -> {key: value}; the agent types <secret>key</secret> and Browser Use substitutes it on that origin only
            req["sensitive_data"] = sensitive
            names = ", ".join(f"<secret>{k}</secret>" for v in sensitive.values() for k in v)
            req["task"] += ("\n\nSaved logins are available. When a login form asks for them, enter these placeholders "
                            f"exactly (they are replaced with the real values on the matching site): {names}.")
        proc = await asyncio.create_subprocess_exec(
            cfg.python, "-I", str(RUNNER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(req).encode("utf-8")),
                                                    timeout=cfg.max_minutes * 60 + 90)
        except BaseException:
            # timeout, cancellation or task interrupt: never leave an agent driving the browser
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        out = _parse(stdout)
        if out is None:
            tail = stderr.decode("utf-8", "replace")[-600:]
            raise JobFailure(ErrorCode.INTERNAL, f"Browser Use exited ({proc.returncode}) without a result: {tail}",
                             uncertain=True)
        if not out.get("ok"):
            code = ErrorCode.TIMEOUT if out.get("error") == "TIMEOUT" else ErrorCode.INTERNAL
            raise JobFailure(code, f"Browser Use failed: {out.get('error')}: {out.get('message')}", uncertain=True)

        final = out.get("final") or {}
        artifacts: list[str] = []
        shot_id = None
        if final.get("screenshot_b64"):
            shot_id = await ctx.upload("screenshot", f"{job.job_id}-final.png", base64.b64decode(final["screenshot_b64"]),
                                       media_type="image/png", meta={"source": "browser_use_final"})
            artifacts.append(shot_id)
        result = {
            "done": out.get("done"), "success": out.get("success"), "final_result": out.get("final_result"),
            "steps": out.get("steps"), "duration_seconds": out.get("duration_seconds"), "urls": out.get("urls", []),
            "errors": out.get("errors", []), "url": final.get("url"), "title": final.get("title"),
            "text": final.get("text", ""), "screenshot_artifact_id": shot_id,
        }
        return HandlerResult(result=result, artifact_ids=artifacts)

    return {"browser.agent_task": agent_task}
