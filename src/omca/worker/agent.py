"""Windows worker agent.

Runs inside the VM's *interactive* session (not a service session) so browser and desktop
automation can reach the desktop. It registers with a fresh session epoch, heartbeats,
long-polls for fenced jobs, executes one bounded operation at a time and returns durable
receipts. A local receipt cache makes execution idempotent per action id and lets the
coordinator reconcile actions whose receipts were lost.
"""

from __future__ import annotations

import asyncio
import json
import platform
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from .. import __version__
from ..config import BrowserAttachment
from ..contracts import (
    ActionReceipt,
    ErrorCode,
    Observation,
    ReceiptError,
    ReceiptStatus,
    WorkerHeartbeat,
    WorkerJob,
    WorkerRegistration,
)
from ..util import now_iso, parse_iso, sha256_bytes, utcnow


class JobFailure(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain


@dataclass
class HandlerResult:
    result: dict[str, Any] = field(default_factory=dict)
    observation: Observation | None = None
    artifact_ids: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)


class WorkerTransport(Protocol):
    async def register(self, reg: WorkerRegistration) -> dict[str, Any]: ...
    async def heartbeat(self, hb: WorkerHeartbeat) -> dict[str, Any]: ...
    async def claim(self, epoch: str, wait_seconds: float) -> WorkerJob | None: ...
    async def submit(self, epoch: str, receipt: ActionReceipt) -> dict[str, Any]: ...
    async def upload(self, job_id: str, output_kind: str, name: str, data: bytes, *, media_type: str | None = None,
                     meta: dict[str, Any] | None = None) -> str: ...
    async def download_input(self, job_id: str, artifact_id: str) -> bytes: ...


@dataclass
class JobContext:
    job: WorkerJob
    transport: WorkerTransport
    workspace: Path
    attachments: dict[str, BrowserAttachment]

    async def upload(self, output_kind: str, name: str, data: bytes, *, media_type: str | None = None,
                     meta: dict[str, Any] | None = None) -> str:
        return await self.transport.upload(self.job.job_id, output_kind, name, data, media_type=media_type, meta=meta)


Handler = Callable[[WorkerJob, JobContext], Awaitable[HandlerResult]]


class WorkerAgent:
    def __init__(
        self,
        worker_id: str,
        transport: WorkerTransport,
        *,
        workspace: Path,
        capabilities: list[str],
        attachments: list[BrowserAttachment] | None = None,
        handlers: dict[str, Handler] | None = None,
        claim_wait_seconds: float = 20.0,
    ):
        self.worker_id = worker_id
        self.transport = transport
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "receipts").mkdir(exist_ok=True)
        (self.workspace / "started").mkdir(exist_ok=True)
        self.capabilities = capabilities
        self.attachments = {a.id: a for a in attachments or []}
        self.handlers: dict[str, Handler] = handlers or {}
        self.handlers.setdefault("worker.reconcile", self._reconcile)
        self.claim_wait_seconds = claim_wait_seconds
        self.epoch = f"{worker_id}-{uuid.uuid4().hex[:12]}"
        self.highest_tokens: dict[str, int] = {}
        self.heartbeat_seconds = 15.0
        self.lease_ttl = 60.0
        self.last_heartbeat_ok = 0.0
        self.clock_skew = 0.0
        self.cancelled: set[str] = set()
        self._running: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------------------------
    async def start(self) -> None:
        reg = WorkerRegistration(
            worker_id=self.worker_id, capabilities=self.capabilities, session_epoch=self.epoch, version=__version__,
            interactive_resources=[f"{self.worker_id}:ui"], attachments=list(self.attachments),
            platform=f"{platform.system()} {platform.release()}",
        )
        info = await self.transport.register(reg)
        self.heartbeat_seconds = float(info.get("heartbeat_seconds", 15))
        self.lease_ttl = float(info.get("lease_ttl_seconds", 60))
        self._sync_clock(info.get("server_time"))
        self.last_heartbeat_ok = time.monotonic()

    def _sync_clock(self, server_time: str | None) -> None:
        st = parse_iso(server_time) if server_time else None
        if st:
            self.clock_skew = (st - utcnow()).total_seconds()

    async def run(self) -> None:
        await self.start()
        await self.run_loop_registered()

    async def run_loop_registered(self) -> None:
        """Heartbeat and claim/execute loop for an already registered agent."""
        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stop.is_set():
                try:
                    job = await self.transport.claim(self.epoch, self.claim_wait_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - transport or stale-session errors: back off and retry
                    await asyncio.sleep(2)
                    continue
                if job is None:
                    continue
                receipt = await self.execute(job)
                await self._submit_with_retry(receipt)
        finally:
            hb.cancel()

    async def run_once(self, wait_seconds: float = 0.5) -> ActionReceipt | None:
        """Claim and execute a single job (tests and the in-process worker)."""
        job = await self.transport.claim(self.epoch, wait_seconds)
        if job is None:
            return None
        receipt = await self.execute(job)
        await self._submit_with_retry(receipt)
        return receipt

    def stop(self) -> None:
        self._stop.set()

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await self.heartbeat()
            await asyncio.sleep(self.heartbeat_seconds)

    async def heartbeat(self) -> None:
        try:
            resp = await self.transport.heartbeat(WorkerHeartbeat(session_epoch=self.epoch, health={"ok": True}))
        except Exception:  # noqa: BLE001 - a failed heartbeat simply ages the connection
            return
        self.last_heartbeat_ok = time.monotonic()
        self._sync_clock(resp.get("server_time"))
        for res, tok in (resp.get("fencing_tokens") or {}).items():
            self.highest_tokens[res] = max(self.highest_tokens.get(res, 0), int(tok))
        for jid in resp.get("cancel_job_ids") or []:
            self.cancelled.add(jid)
            if self._running is not None and getattr(self._running, "omca_job_id", None) == jid:
                self._running.cancel()

    async def _submit_with_retry(self, receipt: ActionReceipt) -> None:
        for delay in (0, 1, 2, 4, 8):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.transport.submit(self.epoch, receipt)
                return
            except (httpx.HTTPError, OSError):
                continue
        # receipt stays in the local cache for reconciliation

    # -- execution -----------------------------------------------------------------------------
    def _receipt_path(self, action_id: str) -> Path:
        return self.workspace / "receipts" / f"{action_id}.json"

    def _started_path(self, action_id: str) -> Path:
        return self.workspace / "started" / f"{action_id}.json"

    def _reject(self, job: WorkerJob, code: ErrorCode, msg: str) -> ActionReceipt:
        now = now_iso()
        return ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=self.worker_id,
                             fencing_token=job.fencing_token, status=ReceiptStatus.REJECTED, started_at=now,
                             finished_at=now, error=ReceiptError(code=code, message=msg),
                             component_versions={"omca-worker": __version__})

    def fence_check(self, job: WorkerJob) -> ActionReceipt | None:
        """Validate the fencing token, lease expiry and connection health before acting."""
        newest = self.highest_tokens.get(job.resource, 0)
        if job.fencing_token < newest:
            return self._reject(job, ErrorCode.STALE_FENCING_TOKEN,
                                f"token {job.fencing_token} is older than {newest} for {job.resource}")
        self.highest_tokens[job.resource] = max(newest, job.fencing_token)
        server_now = utcnow().timestamp() + self.clock_skew
        exp = parse_iso(job.lease_expires_at)
        if exp is None or exp.timestamp() <= server_now:
            return self._reject(job, ErrorCode.STALE_FENCING_TOKEN, "lease expired before execution")
        deadline = parse_iso(job.deadline)
        if deadline is None or deadline.timestamp() <= server_now:
            return self._reject(job, ErrorCode.TIMEOUT, "job deadline passed before execution")
        if time.monotonic() - self.last_heartbeat_ok > self.lease_ttl:
            return self._reject(job, ErrorCode.WORKER_UNAVAILABLE, "lost contact with the coordinator; not acting")
        if job.job_id in self.cancelled:
            return self._reject(job, ErrorCode.CANCELLED, "job was cancelled")
        return None

    async def execute(self, job: WorkerJob) -> ActionReceipt:
        cached = self._receipt_path(job.action_id)
        if cached.exists():
            return ActionReceipt.model_validate_json(cached.read_text(encoding="utf-8"))
        rejection = self.fence_check(job)
        if rejection is not None:
            return rejection
        handler = self.handlers.get(job.operation)
        if handler is None:
            return self._reject(job, ErrorCode.UNSUPPORTED_CAPABILITY, f"worker has no handler for {job.operation}")
        started = now_iso()
        self._started_path(job.action_id).write_text(json.dumps({"job": job.model_dump(), "started_at": started}),
                                                     encoding="utf-8")
        ctx = JobContext(job, self.transport, self.workspace, self.attachments)
        server_now = utcnow().timestamp() + self.clock_skew
        timeout = max(1.0, parse_iso(job.deadline).timestamp() - server_now)  # type: ignore[union-attr]
        task = asyncio.ensure_future(handler(job, ctx))
        task.omca_job_id = job.job_id  # type: ignore[attr-defined]
        self._running = task
        try:
            out = await asyncio.wait_for(task, timeout=timeout)
            receipt = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=self.worker_id,
                                    fencing_token=job.fencing_token, status=ReceiptStatus.SUCCEEDED,
                                    started_at=started, finished_at=now_iso(), result=out.result,
                                    observation=out.observation, artifact_ids=out.artifact_ids,
                                    verification=out.verification, component_versions=self.versions())
        except JobFailure as exc:
            status = ReceiptStatus.UNKNOWN if exc.uncertain else ReceiptStatus.FAILED
            receipt = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=self.worker_id,
                                    fencing_token=job.fencing_token, status=status, started_at=started,
                                    finished_at=now_iso(),
                                    error=ReceiptError(code=exc.code, message=str(exc), retryable=exc.retryable),
                                    component_versions=self.versions())
        except (TimeoutError, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            receipt = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=self.worker_id,
                                    fencing_token=job.fencing_token, status=ReceiptStatus.UNKNOWN,
                                    started_at=started, finished_at=now_iso(),
                                    error=ReceiptError(code=ErrorCode.CANCELLED if cancelled else ErrorCode.TIMEOUT,
                                                       message="interrupted mid-operation; outcome uncertain"),
                                    component_versions=self.versions())
        except Exception as exc:  # noqa: BLE001 - convert to an explicit receipt
            receipt = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id=self.worker_id,
                                    fencing_token=job.fencing_token, status=ReceiptStatus.UNKNOWN,
                                    started_at=started, finished_at=now_iso(),
                                    error=ReceiptError(code=ErrorCode.INTERNAL,
                                                       message=f"{type(exc).__name__}: {exc}"[:500]),
                                    result={"trace": traceback.format_exc()[-1500:]},
                                    component_versions=self.versions())
        finally:
            self._running = None
        cached.write_text(receipt.model_dump_json(), encoding="utf-8")
        return receipt

    def versions(self) -> dict[str, str]:
        out = {"omca-worker": __version__, "python": platform.python_version()}
        try:
            from importlib.metadata import version

            out["playwright"] = version("playwright")
        except Exception:  # noqa: BLE001
            pass
        return out

    async def _reconcile(self, job: WorkerJob, ctx: JobContext) -> HandlerResult:
        """Report what this worker knows about an earlier action (never re-executes it)."""
        action_id = job.arguments["action_id"]
        rp = self._receipt_path(action_id)
        if rp.exists():
            prior = json.loads(rp.read_text(encoding="utf-8"))
            return HandlerResult(result={"known": True, "state": "finished", "receipt": prior})
        if self._started_path(action_id).exists():
            return HandlerResult(result={"known": True, "state": "started_without_receipt"})
        return HandlerResult(result={"known": False, "state": "never_started"})


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class HttpTransport:
    """Worker -> coordinator over HTTPS with a per-worker bearer credential."""

    def __init__(self, base_url: str, worker_id: str, token: str, ca_file: str | None = None, timeout: float = 60):
        self.worker_id = worker_id
        verify: Any = ca_file if ca_file else True
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, verify=verify,
                                        headers={"Authorization": f"Bearer {token}"})

    async def _post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> httpx.Response:
        resp = await self.client.post(path, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(f"{path}: HTTP {resp.status_code} {resp.text[:200]}", request=resp.request,
                                        response=resp)
        return resp

    async def register(self, reg: WorkerRegistration) -> dict[str, Any]:
        return (await self._post("/workers/register", reg.model_dump())).json()

    async def heartbeat(self, hb: WorkerHeartbeat) -> dict[str, Any]:
        return (await self._post(f"/workers/{self.worker_id}/heartbeat", hb.model_dump())).json()

    async def claim(self, epoch: str, wait_seconds: float) -> WorkerJob | None:
        resp = await self._post(f"/workers/{self.worker_id}/jobs/claim",
                                {"session_epoch": epoch, "wait_seconds": wait_seconds}, timeout=wait_seconds + 15)
        if resp.status_code == 204 or not resp.content:
            return None
        return WorkerJob.model_validate(resp.json())

    async def submit(self, epoch: str, receipt: ActionReceipt) -> dict[str, Any]:
        resp = await self.client.post(f"/workers/{self.worker_id}/receipts", json=receipt.model_dump(mode="json"),
                                      headers={"X-OMCA-Session-Epoch": epoch})
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(f"receipt: HTTP {resp.status_code}", request=resp.request, response=resp)
        return resp.json()

    async def upload(self, job_id: str, output_kind: str, name: str, data: bytes, *, media_type: str | None = None,
                     meta: dict[str, Any] | None = None) -> str:
        files = {"file": (name, data, media_type or "application/octet-stream")}
        form = {"job_id": job_id, "output_kind": output_kind,
                "meta": json.dumps({**(meta or {}), "sha256": sha256_bytes(data)})}
        resp = await self.client.post(f"/workers/{self.worker_id}/artifacts", data=form, files=files, timeout=300)
        if resp.status_code >= 400:
            raise JobFailure(ErrorCode.POLICY_DENIED, f"upload refused: {resp.text[:200]}")
        return resp.json()["artifact_id"]

    async def download_input(self, job_id: str, artifact_id: str) -> bytes:
        resp = await self.client.get(f"/workers/{self.worker_id}/jobs/{job_id}/inputs/{artifact_id}", timeout=300)
        if resp.status_code >= 400:
            raise JobFailure(ErrorCode.POLICY_DENIED, f"input refused: {resp.text[:200]}")
        expected = resp.headers.get("X-OMCA-SHA256")
        if not expected or sha256_bytes(resp.content) != expected:
            raise JobFailure(ErrorCode.VERIFICATION_FAILED, "input transfer failed hash verification")
        return resp.content


class InProcessTransport:
    """Same protocol without HTTP: used for the single-VM all-in-one mode and for tests."""

    def __init__(self, coordinator: Any, worker_id: str):
        self.c = coordinator
        self.worker_id = worker_id

    async def register(self, reg: WorkerRegistration) -> dict[str, Any]:
        return self.c.workers.register(reg)

    async def heartbeat(self, hb: WorkerHeartbeat) -> dict[str, Any]:
        return self.c.workers.heartbeat(self.worker_id, hb)

    async def claim(self, epoch: str, wait_seconds: float) -> WorkerJob | None:
        return await self.c.workers.claim_wait(self.worker_id, epoch, wait_seconds)

    async def submit(self, epoch: str, receipt: ActionReceipt) -> dict[str, Any]:
        return self.c.accept_receipt(self.worker_id, epoch, receipt)

    async def upload(self, job_id: str, output_kind: str, name: str, data: bytes, *, media_type: str | None = None,
                     meta: dict[str, Any] | None = None) -> str:
        return self.c.accept_upload(self.worker_id, job_id, output_kind, name, data, media_type=media_type,
                                    meta={**(meta or {}), "sha256": sha256_bytes(data)})

    async def download_input(self, job_id: str, artifact_id: str) -> bytes:
        self.c.workers.validate_input_download(self.worker_id, job_id, artifact_id)
        return self.c.artifacts.read_bytes(artifact_id)
