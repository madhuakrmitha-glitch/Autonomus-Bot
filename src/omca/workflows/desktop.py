"""Desktop and office workflow (P3; FR24): a plan of bounded adapter operations.

The plan is either supplied explicitly (``params.steps``) or proposed by the planning model
from the allowlisted office operations. Every step still passes the policy gate through
``ctx.act``: drafts are local previews, sends and overwrites require owner approval.
"""

from __future__ import annotations

from typing import Any

from ..engine import WorkflowOutcome
from ..util import truncate
from .common import model_json, system_prompt

OFFICE_OPS = {
    "spreadsheet.read": "Read cells: {artifact_id, sheet?, cell_range?}",
    "spreadsheet.write": "Write cells into a NEW copy: {artifact_id, sheet?, cells: {A1: value}, output_name?}",
    "email.draft": "Create a local email preview: {to: [..], subject, body}",
    "email.send": "Send a previously drafted email (owner approval): {draft_artifact_id: '$draft', to: [..]}",
    "calendar.draft": "Create a local calendar preview: {title, start, end, attendees?, location?, description?}",
    "calendar.invite": "Send a drafted invitation (owner approval): {draft_artifact_id: '$draft', attendees: [..]}",
    "file.write": "Save an output to the reports folder: {rel_path, artifact_id: '$output', overwrite?}",
}


async def desktop_workflow(ctx) -> WorkflowOutcome:
    c = ctx.c
    req = ctx.task.request
    inputs = [c.artifacts.get(a) for a in req.attachments]
    inputs = [m for m in inputs if m is not None]
    steps = req.params.get("steps")
    if not steps:
        listing = "\n".join(f"- {m.artifact_id}: {m.name} ({m.media_type})" for m in inputs) or "(none)"
        ops = "\n".join(f"- {k}: {v}" for k, v in OFFICE_OPS.items())
        plan = await model_json(ctx, "planning", [
            {"role": "system", "content": system_prompt(ctx, "You plan office tasks using only bounded adapter operations.")},
            {"role": "user", "content": (
                f"Request: {ctx.task.goal}\n\nInput files:\n{listing}\n\nAvailable operations:\n{ops}\n\n"
                "Return JSON {\"steps\": [{\"operation\": str, \"arguments\": {...}, \"why\": str}]}. Use '$draft' to "
                "refer to the most recent draft and '$output' to the most recent produced file. Keep the plan short.")},
        ], key="plan", default={"steps": []})
        steps = (plan or {}).get("steps") or []
    if not isinstance(steps, list) or not steps:
        return WorkflowOutcome("FAILED", "No executable plan could be formed from the request.", error_code="MALFORMED_TOOL_CALL")

    last_draft, last_output = None, None
    outputs: list[str] = []
    log: list[str] = []
    worker = req.worker_id
    for i, st in enumerate(steps[:15]):
        if not isinstance(st, dict):
            continue
        op = str(st.get("operation") or "")
        if op not in OFFICE_OPS:
            log.append(f"{i + 1}. {op}: refused (not an office adapter operation)")
            ctx.note_limitation(f"step {i + 1} requested {op}, which has no bounded adapter")
            continue
        args: dict[str, Any] = dict(st.get("arguments") or {})
        for k, v in list(args.items()):
            if v == "$draft":
                args[k] = last_draft
            elif v == "$output":
                args[k] = last_output
        ctx.progress(f"{op} ({i + 1}/{len(steps)})", phase="office")
        out = await ctx.act(op, args, key=f"step:{i}", worker_id=worker, rationale=str(st.get("why") or ""))
        log.append(f"{i + 1}. {op}: {out.status.value.lower()}" + (f" - {out.message or out.reason}" if not out.ok else ""))
        if out.ok:
            if out.result.get("draft_artifact_id"):
                last_draft = out.result["draft_artifact_id"]
                outputs.append(last_draft)
            if op == "spreadsheet.write" and out.result.get("artifact_id"):
                last_output = out.result["artifact_id"]
                outputs.append(last_output)
            if op == "spreadsheet.read":
                vals = out.result.get("values") or {}
                log.append(f"   read {len(vals)} cell(s) from {out.result.get('sheet')}")
        elif out.status.value == "DENIED":
            ctx.note_limitation(f"step {i + 1} ({op}) was not executed: {out.message or out.reason}")
    for k, art in enumerate(outputs):
        m = c.artifacts.get(art)
        if m is not None:
            await ctx.act("telegram.send_owner", {"text": f"Output: {m.name}", "artifact_id": art}, key=f"deliver:{k}")
    executed = [ln for ln in log if ": succeeded" in ln]
    summary = "Office workflow steps:\n" + "\n".join(log)
    status = "COMPLETED" if executed and len(executed) == sum(1 for ln in log if not ln.startswith("   ")) else "PARTIAL"
    return WorkflowOutcome(status, truncate(summary, 3500), artifacts=outputs, limitations=list(ctx.limitations),
                           details={"steps": log}, checks_passed=status == "COMPLETED")
