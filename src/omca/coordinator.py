"""The coordinator: one orchestrator that owns task state, policy, budget, model routing,
workers, evidence and owner notifications (spec sections 5 and 6).

Owner entry points here assume the caller has already authenticated the owner (the channel
layer or the owner API token). Worker entry points assume the worker credential was checked.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, ArtifactStore
from .budget import BudgetLedger
from .channels import Channel, Notifier
from .channels.web import WebInbox
from .config import ApplicationConfig, BrowserAttachment
from .contracts import (
    ActionIntent,
    ActionReceipt,
    ElementRef,
    ErrorCode,
    PolicyDecision,
    TaskRequest,
    TaskStatus,
    WorkerJob,
)
from .db import Database
from .engine import ActionFailure, TaskContext, TaskEngine
from .evidence import EvidenceStore, ObservationStore
from .memory import MemoryStore
from .models import ModelRegistry, ModelRouter
from .models.providers import Provider
from .policy import ApprovalError, ApprovalService, Identity, PolicyGate
from .research.search import SearchService
from .tasks import TaskRecord, TaskStore, TransitionError
from .util import digest, display_time, iso, new_id, now_iso, parse_iso, sha256_bytes, truncate, utcnow
from .workers import WorkerError, WorkerManager, job_deadline

S = TaskStatus

WORKFLOW_RESULTS = {
    "research_report": "a cited Word report saved to your reports folder and sent here",
    "video_analysis": "timestamped video findings (frames + transcript) in a Word report",
    "browser_task": "the requested browser outcome, verified on the page, with a short summary",
    "desktop_workflow": "the requested office/desktop outcome through a bounded adapter",
}

UPLOAD_KIND = {"screenshot": "observation", "frame": "media", "image": "evidence", "download": "input",
               "media": "media", "subtitles": "media", "draft": "output", "workbook": "output"}

VIDEO_HINT = re.compile(r"(youtube\.com|youtu\.be|vimeo\.com|\.mp4\b|\.webm\b|\bvideo\b|\bclip\b|\blecture recording\b)", re.I)
BROWSE_HINT = re.compile(r"^(go to|open|log ?in|sign ?in|click|fill|navigate|browse|book|order|download)\b", re.I)
DESKTOP_HINT = re.compile(r"\b(spreadsheet|excel|xlsx|workbook|email draft|draft an email|calendar invite|meeting invite)\b", re.I)


def classify_workflow(goal: str) -> str:
    if VIDEO_HINT.search(goal):
        return "video_analysis"
    if DESKTOP_HINT.search(goal):
        return "desktop_workflow"
    if BROWSE_HINT.search(goal.strip()):
        return "browser_task"
    return "research_report"


class Coordinator:
    def __init__(self, config: ApplicationConfig, *, channel: Channel | None = None,
                 providers: dict[str, Provider] | None = None, search: SearchService | None = None):
        self.config = config
        self.db = Database(config.storage.database_path)
        self.tasks = TaskStore(self.db)
        self.artifacts = ArtifactStore(self.db, config)
        self.ledger = BudgetLedger(self.db, config)
        self.ledger.on_overrun = self._budget_overrun
        self._pause_attempts: dict[str, float] = {}
        self.registry = ModelRegistry(self.db, config, self.ledger)
        self.router = ModelRouter(config, self.registry, self.ledger, providers)
        self.policy = PolicyGate(self.db, config)
        self.approvals = ApprovalService(self.db, config)
        self.workers = WorkerManager(self.db, config)
        self.evidence = EvidenceStore(self.db, config.reports.max_excerpt_chars)
        self.observations = ObservationStore(self.db)
        self.memory = MemoryStore(self.db)
        self.search = search or SearchService.from_config(config)
        self.web_inbox = WebInbox(self.db) if config.ui.enabled else None
        self.notifier = Notifier(self.db, config, channel, self.web_inbox)
        from .workflows import WORKFLOWS

        self.engine = TaskEngine(self, WORKFLOWS)
        from .scheduler import Scheduler

        self.scheduler = Scheduler(self)
        self._stop = asyncio.Event()
        self._loops: list[asyncio.Task] = []
        self.in_process_workers: list[Any] = []

    # -- identity -----------------------------------------------------------------------------------
    @property
    def owner_key(self) -> str:
        ids = self.notifier.owner_ids()
        return f"telegram:{ids[0]}" if ids else "owner"

    def is_owner(self, identity: Identity) -> bool:
        if identity.channel == "owner_api":
            return True
        if identity.channel == "telegram":
            return identity.user_id in {str(i) for i in self.notifier.owner_ids()}
        return False

    def cmd(self, name: str) -> str:
        """Command spelling for owner messages (OpenClaw reserves /status, /approve, ... for itself)."""
        if self.config.telegram.mode == "openclaw" and name in {"status", "pause", "resume", "cancel", "approve", "deny"}:
            return f"/omca {name}"
        return f"/{name}"

    def attachment(self, attachment_id: str | None) -> BrowserAttachment | None:
        if attachment_id is None:
            return None
        return next((a for a in self.config.browser.attachments if a.id == attachment_id), None)

    def default_attachment(self, worker_id: str | None = None) -> BrowserAttachment | None:
        for a in self.config.browser.attachments:
            if a.authorized and (worker_id is None or a.worker_id == worker_id):
                return a
        return None

    # -- lifecycle -------------------------------------------------------------------------------------
    async def start(self) -> None:
        self.ledger.recover_after_restart()
        self.scheduler.sync_from_config()
        self._stop.clear()
        self._loops = [
            asyncio.ensure_future(self.engine.run_loop(self._stop)),
            asyncio.ensure_future(self.notifier.run(self._stop)),
            asyncio.ensure_future(self._maintenance_loop()),
            asyncio.ensure_future(self.scheduler.run(self._stop)),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self.engine.runners.values():
            t.cancel()
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, *self.engine.runners.values(), return_exceptions=True)
        self._loops = []

    async def _maintenance_loop(self) -> None:
        last_retention = 0.0
        while not self._stop.is_set():
            try:
                self.maintenance()
                await self.enforce_endpoint_windows()
                now = asyncio.get_running_loop().time()
                if now - last_retention > 3600:
                    self.apply_retention()
                    last_retention = now
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except TimeoutError:
                pass

    def maintenance(self) -> None:
        self.approvals.expire_due()
        self.workers.mark_offline_workers()
        for t in self.ledger.new_threshold_notices():
            st = self.ledger.status()
            self.notifier.text(f"Budget notice: {t:.0%} of the monthly model budget is committed "
                               f"({st.ceiling - st.remaining:.2f} of {st.ceiling:.2f} {st.currency}).",
                               key=f"budget:{st.period}:{t}")
        interval = self.config.limits.progress_interval_seconds
        for t in self.tasks.list([S.RUNNING, S.VERIFYING], limit=100):
            last = parse_iso(t.last_progress_at) if t.last_progress_at else parse_iso(t.updated_at)
            if last and (utcnow() - last).total_seconds() >= interval:
                self.send_progress(t)

    def _budget_overrun(self, call_id: str, route_id: str, reserved: float, actual: float) -> None:
        cur = self.config.budget.currency
        self.notifier.text(f"Budget notice: a call on {route_id} cost {actual:.2f} {cur}, more than the {reserved:.2f} "
                           f"{cur} reserved for it. The ledger records the actual cost; check the route's prices.",
                           key=f"overrun:{call_id}")

    async def enforce_endpoint_windows(self) -> None:
        """Pause dedicated endpoints whose approved window ended; settle only once a pause succeeded.

        Until then the session stays open, its running cost keeps counting against the budget, the
        pause is retried on every maintenance tick and the owner is reminded every 15 minutes.
        """
        from .models.endpoints import controller_for

        for session_id, route_id in self.ledger.overdue_endpoint_sessions():
            route = self.config.models.route(route_id)
            if route is None:
                continue
            last = self._pause_attempts.get(session_id, 0.0)
            if time.monotonic() - last < 60:
                continue  # at most one pause attempt per minute per session
            self._pause_attempts[session_id] = time.monotonic()
            try:
                await controller_for(route).pause(route)
            except Exception as exc:  # noqa: BLE001 - keep the session open and keep trying
                cost = self.ledger.accrue_endpoint_session(session_id, route)
                bucket = int(utcnow().timestamp() // 900)
                self.notifier.text(
                    f"Dedicated endpoint {route_id} is past its approved window and pausing FAILED ({truncate(str(exc), 120)}). "
                    f"Pause it manually now. It has cost about {cost:.2f} {self.config.budget.currency} so far and "
                    "keeps counting against the budget; OMCA retries every minute.",
                    key=f"eps-fail:{session_id}:{bucket}")
                continue
            self._pause_attempts.pop(session_id, None)
            cost = self.ledger.settle_endpoint_session(session_id, route)
            self.notifier.text(f"Dedicated endpoint {route_id} reached the end of its approved window; it was paused "
                               f"and its running time settled ({cost:.2f} {self.config.budget.currency}).",
                               key=f"eps:{session_id}")

    def apply_retention(self) -> dict[str, int]:
        r = self.config.retention
        expired = self.artifacts.expire()
        obs_cut = iso(utcnow() - timedelta(days=r.observation_days))
        purged = self.observations.purge_before(obs_cut)  # type: ignore[arg-type]
        act_cut = iso(utcnow() - timedelta(days=r.action_log_days))
        events = self.db.execute("DELETE FROM task_events WHERE ts < ?", (act_cut,)).rowcount
        led_cut = iso(utcnow() - timedelta(days=31 * r.usage_ledger_months))
        ledger = self.db.execute("DELETE FROM model_calls WHERE created_at < ? AND status IN ('SETTLED','RELEASED','REFUSED')",
                                 (led_cut,)).rowcount
        return {"artifacts": expired, "observations": purged, "events": events, "ledger_rows": ledger}

    # -- owner entry points -------------------------------------------------------------------------------
    def submit_task(self, request: TaskRequest, *, origin: str = "owner", dedup_key: str | None = None,
                    schedule_run_id: str | None = None, notify: bool = True) -> TaskRecord:
        workflow = request.workflow if request.workflow != "auto" else classify_workflow(request.goal)
        missing = self.missing_inputs(workflow, request)
        limits = {
            "max_actions": request.budget.max_actions or self.config.limits.max_actions_per_task,
            "max_active_minutes": request.budget.max_active_minutes or self.config.limits.max_task_active_minutes,
            "max_model_spend": request.budget.max_model_spend or self.config.budget.per_task_reservation_ceiling,
        }
        task = self.tasks.create(request, owner=self.owner_key, workflow=workflow, limits=limits, origin=origin,
                                 dedup_key=dedup_key, schedule_run_id=schedule_run_id)
        if notify:
            extra = f"\nMissing input: {missing}" if missing else ""
            msg = (f"Task {task.id[-6:]} accepted ({workflow.replace('_', ' ')}).\n"
                   f"Intended result: {WORKFLOW_RESULTS.get(workflow, 'a verified result')}.{extra}\n"
                   f"Use {self.cmd('status')} {task.id[-6:]}, {self.cmd('pause')} {task.id[-6:]} or "
                   f"{self.cmd('cancel')} {task.id[-6:]}.")
            ref = self.notifier.text(msg, task_id=task.id, key=f"ack:{task.id}")
            self.tasks.update(task.id, status_message_ref=ref, last_progress_at=now_iso())
        return self.tasks.require(task.id)

    @staticmethod
    def missing_inputs(workflow: str, request: TaskRequest) -> str | None:
        if workflow == "video_analysis" and not (request.params.get("url") or re.search(r"https?://\S+", request.goal)):
            return "a video URL (I will ask for it)"
        return None

    def pause_task(self, task_id: str) -> TaskRecord:
        t = self.tasks.require(task_id)
        if t.terminal or t.status == S.PAUSED:
            return t
        t = self.tasks.transition(task_id, S.PAUSED, reason="paused by owner")
        self.engine.interrupt(task_id)
        self.notifier.text(f"Task {task_id[-6:]} paused. No new actions will start. Use {self.cmd('resume')} {task_id[-6:]}.",
                           task_id=task_id, key=f"pause:{task_id}:{t.checkpoint_version}")
        return t

    def resume_task(self, task_id: str, *, extra_budget: float | None = None) -> TaskRecord:
        t = self.tasks.require(task_id)
        if t.status == S.PAUSED or t.status == S.BLOCKED:
            if extra_budget:
                req = t.request.model_copy(update={"budget": t.request.budget.model_copy(
                    update={"max_model_spend": (t.request.budget.max_model_spend or self.config.budget.per_task_reservation_ceiling) + extra_budget})})
                self.tasks.update(task_id, request=req)
            t = self.tasks.transition(task_id, S.QUEUED, reason="resumed by owner")
            self.notifier.text(f"Task {task_id[-6:]} resumed.", task_id=task_id, key=f"resume:{task_id}:{t.checkpoint_version}")
        return t

    def cancel_task(self, task_id: str) -> TaskRecord:
        t = self.tasks.require(task_id)
        if t.terminal:
            return t
        t = self.tasks.transition(task_id, S.CANCELLED, reason="cancelled by owner")
        self.approvals.invalidate_task(task_id, "task cancelled")
        for j in self.workers.open_jobs():
            if j["task_id"] == task_id:
                self.workers.cancel_job(j["id"])
        self.engine.interrupt(task_id)
        if task_id not in self.engine.runners or self.engine.runners[task_id].done():
            self.on_interrupted(t, "cancel")
        return t

    def steer_task(self, task_id: str, text: str) -> None:
        t = self.tasks.require(task_id)
        if t.terminal:
            raise TransitionError(f"task {task_id[-6:]} is already {t.status.value.lower()}")
        self.tasks.add_message(task_id, "steer", text)
        self.notifier.text(f"Task {task_id[-6:]}: noted - \"{truncate(text, 200)}\". Completed evidence and actions are kept.",
                           task_id=task_id)

    def answer_task(self, task_id: str, text: str) -> None:
        self.tasks.add_message(task_id, "owner_input", text)

    def decide_approval(self, approval_id: str, identity: Identity, decision: str) -> str:
        try:
            ap = self.approvals.decide(approval_id, identity, decision)
        except ApprovalError as exc:
            return f"Not accepted: {exc}"
        ref = self.approvals.message_ref(approval_id)
        verdict = "approved (once)" if ap.status.value == "APPROVED" else "denied"
        out_ref = f"out_{digest(f'approval:{approval_id}')[:24]}"
        self.notifier.edit(ref or out_ref, f"{ap.preview}\n\nDecision: {verdict}.", task_id=ap.task_id,
                           key=f"approval-edit:{approval_id}")
        return f"Action {verdict}."

    # -- worker entry points ----------------------------------------------------------------------------------
    def accept_receipt(self, worker_id: str, epoch: str, receipt: ActionReceipt) -> dict[str, Any]:
        return self.workers.submit_receipt(worker_id, epoch, receipt)

    def accept_upload(self, worker_id: str, job_id: str, output_kind: str, name: str, data: bytes, *,
                      media_type: str | None, meta: dict[str, Any]) -> str:
        job = self.workers.validate_upload(worker_id, job_id, output_kind)
        declared_sha = meta.get("sha256")
        if declared_sha and declared_sha != sha256_bytes(data):
            raise WorkerError(ErrorCode.VERIFICATION_FAILED, "uploaded bytes do not match the declared sha256")
        dest = meta.get("destination")
        if dest is not None and (not isinstance(dest, str) or dest.startswith(("/", "\\")) or ".." in dest.replace("\\", "/").split("/")
                                 or ":" in dest):
            raise WorkerError(ErrorCode.POLICY_DENIED, "artifact destination must be a plain name inside the task workspace")
        kind = UPLOAD_KIND.get(output_kind, "output")
        mt = media_type if media_type and media_type != "application/octet-stream" else None
        try:
            if mt and mt.startswith("image/"):
                stored = self.artifacts.put_image(job.task_id, kind, name, data, media_type=mt,
                                                  source_map={"job_id": job_id, "worker_id": worker_id, **meta})
            else:
                stored = self.artifacts.put(job.task_id, kind, name, data, media_type=mt,
                                            source_map={"job_id": job_id, "worker_id": worker_id, **meta})
        except ArtifactError as exc:
            raise WorkerError(ErrorCode.UNSUPPORTED_MEDIA, str(exc)) from exc
        return stored.manifest.artifact_id

    # -- coordinator-side execution --------------------------------------------------------------------------
    def target_state(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Current target state for approval preconditions of non-browser operations."""
        if operation in ("file.write", "file.delete"):
            p = self._report_path(arguments["rel_path"])
            return {"path": str(p), "exists": p.exists(), "sha256": sha256_bytes(p.read_bytes()) if p.exists() else None}
        if operation in ("email.send", "calendar.invite"):
            m = self.artifacts.get(arguments.get("draft_artifact_id", ""))
            return {"draft_sha256": m.sha256 if m else None}
        if operation == "spreadsheet.write":
            m = self.artifacts.get(arguments.get("artifact_id", ""))
            return {"workbook_sha256": m.sha256 if m else None}
        return {"destination": arguments.get("recipient") or arguments.get("target")}

    def describe_action(self, intent: ActionIntent, decision: PolicyDecision, element: ElementRef | None) -> str:
        a = intent.arguments
        if intent.operation == "browser.click" and element:
            return f"Click {element.role} '{truncate(element.name, 80)}' on {decision.destination or '-'}"
        if intent.operation == "browser.click_point":
            what = f"{element.role} '{truncate(element.name, 60)}'" if element else "an unidentified control"
            return f"Click at ({a['x']:.0f}, {a['y']:.0f}) on {what}"
        if intent.operation == "browser.type":
            return f"Type {len(a.get('text', ''))} characters{' and submit' if a.get('submit') else ''}"
        if intent.operation == "browser.press":
            return f"Press {a['key']}"
        if intent.operation == "email.send":
            return self._email_preview(a)
        if intent.operation == "file.write":
            return f"Overwrite {a['rel_path']}"
        return f"{intent.operation} {truncate(str(a), 160)}"

    def _email_preview(self, a: dict[str, Any]) -> str:
        """What the owner approves: recipients, subject and the body as drafted."""
        from email import policy as email_policy
        from email.parser import BytesParser

        head = f"Send an email to {', '.join(a['to'])}"
        try:
            msg = BytesParser(policy=email_policy.default).parsebytes(self.artifacts.read_bytes(a["draft_artifact_id"]))
            part = msg.get_body(preferencelist=("plain",))
            body = part.get_content() if part is not None else ""
        except Exception:  # noqa: BLE001 - the worker re-checks the draft before sending
            return f"{head}\n(the draft could not be read for preview)"
        others = [h for h in ("Cc", "Bcc") if msg.get_all(h)]
        warning = f"\nWARNING: the draft also has {' and '.join(others)} recipients and will be refused." if others else ""
        return f"{head}\nSubject: {truncate(str(msg['Subject'] or ''), 200)}{warning}\n\n{truncate(body.strip(), 1500)}"

    def _report_path(self, rel_path: str) -> Path:
        base = Path(self.config.storage.owner_reports_dir)
        base.mkdir(parents=True, exist_ok=True)
        p = (base / rel_path).resolve()
        if not p.is_relative_to(base.resolve()):
            raise ActionFailure(ErrorCode.POLICY_DENIED, "output paths must stay inside the reports folder")
        return p

    async def execute_local(self, operation: str, args: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
        if operation == "search.query":
            try:
                results = await self.search.search(args["query"], args.get("limit", 8))
            except Exception as exc:  # noqa: BLE001
                raise ActionFailure(ErrorCode.SITE_UNAVAILABLE, f"search failed: {exc}"[:300], retryable=True) from exc
            return {"results": [r.model_dump() for r in results], "provider": self.search.search_adapter.name}
        if operation == "web.retrieve":
            try:
                page = await self.search.retrieve(args["url"], args.get("formats") or ["markdown"])
            except Exception as exc:  # noqa: BLE001
                raise ActionFailure(ErrorCode.SITE_UNAVAILABLE, f"retrieval failed: {exc}"[:300], retryable=False) from exc
            return {"page": page.model_dump()}
        if operation == "web.fetch_image":
            from .research.search import fetch_image, guard_for

            try:
                data, media = await fetch_image(args["src"], self.config.search.http.user_agent, guard=guard_for(self.config))
            except Exception as exc:  # noqa: BLE001
                raise ActionFailure(ErrorCode.SITE_UNAVAILABLE, str(exc)[:300], retryable=False) from exc
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(media)
            if ext is None:
                raise ActionFailure(ErrorCode.UNSUPPORTED_MEDIA, f"unsupported image type {media}")
            try:
                stored = self.artifacts.put_image(ctx.task_id, "evidence", (args.get("name") or "image") + ext, data,
                                                  media_type=media, source_map={"source_url": args["src"]})
            except (ArtifactError, OSError) as exc:
                raise ActionFailure(ErrorCode.UNSUPPORTED_MEDIA, f"image could not be decoded: {exc}") from exc
            return {"artifact_id": stored.manifest.artifact_id, "source_url": args["src"], "bytes": len(data)}
        if operation == "file.write":
            src = self.artifacts.get(args["artifact_id"])
            if src is None:
                raise ActionFailure(ErrorCode.INTERNAL, "unknown artifact")
            target = self._report_path(args["rel_path"])
            if target.exists() and not args.get("overwrite"):
                stem, suf = target.stem, target.suffix
                i = 2
                while (target.with_name(f"{stem}-{i}{suf}")).exists():
                    i += 1
                target = target.with_name(f"{stem}-{i}{suf}")
            data = self.artifacts.read_bytes(args["artifact_id"])
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            if sha256_bytes(target.read_bytes()) != src.sha256:
                raise ActionFailure(ErrorCode.VERIFICATION_FAILED, "saved file checksum does not match")
            return {"path": str(target), "sha256": src.sha256, "bytes": src.bytes}
        if operation == "file.delete":
            target = self._report_path(args["rel_path"])
            existed = target.exists()
            target.unlink(missing_ok=True)
            return {"deleted": existed, "path": str(target)}
        if operation == "telegram.send_owner":
            # stable across processes so a replayed delivery step hits the same outbox row
            key = f"deliver:{ctx.task_id}:{args.get('artifact_id') or ''}:{digest(args['text'])[:16]}"
            if args.get("artifact_id"):
                m = self.artifacts.get(args["artifact_id"])
                if m is None:
                    raise ActionFailure(ErrorCode.INTERNAL, "unknown artifact")
                oid = self.notifier.document(m.name, self.artifacts.read_bytes(m.artifact_id), args["text"],
                                             task_id=ctx.task_id, key=key)
            else:
                oid = self.notifier.text(args["text"], task_id=ctx.task_id, key=key)
            sent = await self._await_outbox(oid, 60 if not self.config.testing else 10)
            return {"outbox_id": oid, "delivered": sent is not None, "message": sent}
        if operation == "message.send":
            contact = next((c for c in self.config.policy.contacts if c.address == args["recipient"]), None)
            if contact is None or self.notifier.channel is None:
                raise ActionFailure(ErrorCode.POLICY_DENIED, "recipient is not a configured contact")
            try:
                res = await self.notifier.channel.send_text(contact.address, args["text"])
            except Exception as exc:  # noqa: BLE001 - may have been delivered
                raise ActionFailure(ErrorCode.UNCERTAIN_SIDE_EFFECT, f"send outcome unknown: {exc}", uncertain=True) from exc
            return {"sent": True, "message": res}
        raise ActionFailure(ErrorCode.UNSUPPORTED_CAPABILITY, f"no coordinator executor for {operation}")

    async def _await_outbox(self, outbox_id: str, timeout: float) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await self.notifier.flush()
            res = self.notifier.result(outbox_id)
            if res is not None:
                return res
            await asyncio.sleep(0.5)
        return None

    async def release_browser(self, ctx: TaskContext) -> None:
        """Close the task's tab (housekeeping, read-only)."""
        rows = self.db.query("SELECT DISTINCT worker_id, target_json FROM actions WHERE task_id=? AND operation LIKE 'browser.%'"
                             " AND worker_id IS NOT NULL", (ctx.task_id,))
        from .db import loads

        for r in rows:
            att_id = (loads(r["target_json"], {}) or {}).get("attachment_id")
            att = self.attachment(att_id)
            resource = att.lease_resource if att else f"{r['worker_id']}:ui"
            try:
                lease = self.workers.acquire_lease(ctx.task_id, r["worker_id"], resource)
            except WorkerError:
                continue
            job = WorkerJob(job_id=new_id("job"), task_id=ctx.task_id, action_id=new_id("act"), worker_id=r["worker_id"],
                            lease_id=lease.lease_id, resource=resource, fencing_token=lease.fencing_token,
                            operation="browser.release", idempotency_key=new_id("rel"), deadline=job_deadline(15),
                            lease_expires_at=lease.expires_at, issued_at=now_iso(), attachment_id=att_id)
            try:
                await self.workers.dispatch(job, 15)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.workers.release_lease(lease.lease_id)

    # -- notifications -----------------------------------------------------------------------------------------
    def status_text(self, t: TaskRecord) -> str:
        tz = self.config.owner.display_timezone
        lines = [f"Task {t.id[-6:]} - {t.status.value.lower().replace('_', ' ')}", f"Goal: {truncate(t.goal, 200)}"]
        if t.phase or t.current_step:
            lines.append(f"Step: {t.phase or ''} {t.current_step or ''}".strip())
        if t.worker_id:
            lines.append(f"Worker: {t.worker_id}")
        if t.blocking_reason:
            lines.append(f"Blocked by: {t.blocking_reason}")
        lines.append(f"Actions: {t.action_count}; active time {t.active_ms / 60000:.1f} min; "
                     f"model spend {self.ledger.task_spend(t.id):.2f} {self.config.budget.currency}")
        lines.append(f"Updated: {display_time(t.updated_at, tz)}")
        return "\n".join(lines)

    def send_progress(self, t: TaskRecord) -> None:
        text = self.status_text(t)
        if t.status_message_ref:
            self.notifier.edit(t.status_message_ref, text, task_id=t.id)
        else:
            ref = self.notifier.text(text, task_id=t.id)
            self.tasks.update(t.id, status_message_ref=ref)
        self.tasks.update(t.id, last_progress_at=now_iso())

    def on_status_change(self, t: TaskRecord, reason: str | None) -> None:
        if t.status in (S.RUNNING, S.AWAITING_APPROVAL):
            # running is reported through progress; approvals send their own message
            self.tasks.update(t.id, last_progress_at=now_iso())
            return
        label = {S.AWAITING_USER: "is waiting for your input", S.BLOCKED: "is blocked", S.VERIFYING: "is verifying its results",
                 S.PAUSED: "is paused", S.QUEUED: "is queued"}.get(t.status, t.status.value.lower())
        self.notifier.text(f"Task {t.id[-6:]} {label}{': ' + reason if reason else '.'}", task_id=t.id,
                           key=f"status:{t.id}:{t.checkpoint_version}")
        self.tasks.update(t.id, last_progress_at=now_iso())

    def on_finished(self, t: TaskRecord) -> None:
        res = t.result or {}
        lines = [f"Task {t.id[-6:]} {t.status.value.lower()}.", truncate(res.get("summary") or "", 1500)]
        lims = res.get("limitations") or []
        if lims:
            lines.append("Limitations:\n" + "\n".join(f"- {truncate(x, 200)}" for x in lims[:8]))
        if t.error_code and t.status != S.COMPLETED:
            lines.append(f"Reason code: {t.error_code}")
        self.notifier.text("\n\n".join(x for x in lines if x), task_id=t.id, key=f"final:{t.id}")

    def on_interrupted(self, t: TaskRecord, kind: str) -> None:
        done = self.db.query("SELECT operation, status FROM actions WHERE task_id=? AND status IN ('SUCCEEDED','UNKNOWN')",
                             (t.id,))
        effects = [f"{r['operation']} ({r['status'].lower()})" for r in done if not r["operation"].startswith(("browser.observe", "browser.hit_test", "search.", "web."))]
        summary = "; ".join(effects[-8:]) if effects else "no external effects"
        if kind == "cancel":
            self.notifier.text(f"Task {t.id[-6:]} cancelled. Completed effects kept: {summary}.", task_id=t.id,
                               key=f"cancelled:{t.id}")
        else:
            self.tasks.event(t.id, "paused_at_checkpoint", {"effects": effects})
