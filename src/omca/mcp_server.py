"""MCP server exposed to OpenClaw (stdio).

OpenClaw's agent gets only these task-level tools (configure ``tools.allow: ["omca__*"]``);
it never receives raw browser, shell, desktop or network tools. Each tool calls the
coordinator API with the owner API token, so every effect still passes the coordinator's
policy gate, approvals and budget. No tool accepts an owner identity: the OpenClaw plugin's
``before_tool_call`` hook blocks non-owner senders before these tools run.

``task_image`` returns real image content so the integration spike can prove that image
observations survive the MCP return path and reach an image-capable model.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.utilities.types import Image
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.utilities.types import Image  # type: ignore[no-redef]


def build_server(base_url: str | None = None, token: str | None = None) -> Any:
    base = (base_url or os.environ.get("OMCA_URL") or "http://127.0.0.1:8765").rstrip("/")
    tok = token or os.environ.get("OMCA_OWNER_TOKEN") or ""
    verify: Any = os.environ.get("OMCA_CA_FILE") or True
    server = _Server("omca", instructions=(
        "Tools for the owner's Open Model Computer Assistant. Create durable tasks for research reports, video "
        "analysis, browser and office work; check status; pause, resume, cancel or steer tasks. Approvals for "
        "consequential actions are sent to the owner separately and cannot be granted through these tools."))

    def call(method: str, path: str, **kw: Any) -> Any:
        with httpx.Client(base_url=base, timeout=60, verify=verify,
                          headers={"Authorization": f"Bearer {tok}"}) as client:
            resp = client.request(method, path, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"coordinator returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content and resp.headers.get("content-type", "").startswith("application/json") else resp

    def brief(view: dict[str, Any]) -> dict[str, Any]:
        keys = ("task_id", "status", "workflow", "goal", "phase", "current_step", "blocking_reason", "pending_approval_id")
        out = {k: view.get(k) for k in keys}
        if view.get("result"):
            out["result"] = {k: view["result"].get(k) for k in ("summary", "limitations", "checks_passed")}
        out["artifacts"] = [{"artifact_id": a["artifact_id"], "name": a["name"], "sha256": a["sha256"]}
                            for a in view.get("artifacts", [])]
        return out

    @server.tool(description="Create a durable task. workflow: auto, general_workflow, research_report, video_analysis, "
                             "browser_task, desktop_workflow, chat_workflow. The owner receives acknowledgement, progress and results in Telegram.")
    def create_task(goal: str, workflow: str = "auto", sources: list[str] | None = None, url: str | None = None,
                    question: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if url:
            params["url"] = url
        if question:
            params["question"] = question
        view = call("POST", "/tasks", json={"goal": goal, "workflow": workflow, "sources": sources or [], "params": params})
        return brief(view)

    @server.tool(description="Current status, step, worker, blocking reason and results of a task (id or last 6 chars).")
    def task_status(task_id: str) -> dict[str, Any]:
        return brief(call("GET", f"/tasks/{task_id}"))

    @server.tool(description="List recent tasks.")
    def list_tasks(limit: int = 10) -> list[dict[str, Any]]:
        return [brief(v) for v in call("GET", "/tasks", params={"limit": limit})]

    @server.tool(description="Cancel a task: stops future work; completed effects are kept and reported.")
    def cancel_task(task_id: str) -> dict[str, Any]:
        return brief(call("POST", f"/tasks/{task_id}/cancel"))

    @server.tool(description="Pause a task at its next checkpoint.")
    def pause_task(task_id: str) -> dict[str, Any]:
        return brief(call("POST", f"/tasks/{task_id}/pause"))

    @server.tool(description="Resume a paused or blocked task.")
    def resume_task(task_id: str) -> dict[str, Any]:
        return brief(call("POST", f"/tasks/{task_id}/resume", json={}))

    @server.tool(description="Add a follow-up instruction to an active task (completed evidence is preserved).")
    def steer_task(task_id: str, instruction: str) -> dict[str, Any]:
        return brief(call("POST", f"/tasks/{task_id}/steer", json={"text": instruction}))

    @server.tool(description="Monthly model budget: committed, reserved, remaining and enabled hosted routes.")
    def budget() -> dict[str, Any]:
        return call("GET", "/budget")

    @server.tool(description="Return a task's report figure or latest browser screenshot as an image. Only use when "
                             "the owner explicitly asks to see it.")
    def task_image(task_id: str, artifact_id: str | None = None) -> Any:
        view = call("GET", f"/tasks/{task_id}")
        target = artifact_id
        if target is None:
            raise RuntimeError("pass the artifact_id of an image (see task_status artifacts)")
        resp = call("GET", f"/artifacts/{target}")
        media = resp.headers.get("content-type", "image/png").split(";")[0]
        if not media.startswith("image/"):
            raise RuntimeError(f"artifact {target} is not an image ({media}); task {view.get('task_id')}")
        return Image(data=resp.content, format=media.split("/", 1)[1])

    return server


def main() -> None:
    build_server().run()


def image_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


if __name__ == "__main__":
    main()
