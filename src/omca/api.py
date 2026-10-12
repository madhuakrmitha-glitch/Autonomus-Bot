"""Controller and worker adapter API (``contracts/openapi.yaml``; spec section 19).

Authentication per caller class, all secrets by reference:

* owner API / MCP sidecar: ``server.owner_api_token_ref``
* OpenClaw bridge (plugin forwarding verified sender identity): ``telegram.openclaw.bridge_token_ref``
* workers: per-worker ``workers[].credential_ref``; a worker may only act as itself
* model gateway (framework-owned model calls): ``server.model_gateway_token_ref``

No route accepts an owner identity from a request body. Expose this API on a private
network with TLS; never publish browser debugging ports or model endpoints.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .contracts import (
    ActionReceipt,
    ApprovalDecision,
    ApprovalRequest,
    ArtifactManifest,
    BudgetStatus,
    TaskRequest,
    TaskView,
    WorkerHeartbeat,
    WorkerJob,
    WorkerRegistration,
)
from .coordinator import Coordinator
from .models import gateway
from .policy import Identity
from .secrets import resolve
from .tasks import TransitionError
from .workers import WorkerError


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip()


def _match(token: str | None, ref: str | None) -> bool:
    expected = resolve(ref)
    return bool(token and expected and hmac.compare_digest(token.encode(), expected.encode()))


class ClaimRequest(BaseModel):
    session_epoch: str
    wait_seconds: float = 20.0


class SteerRequest(BaseModel):
    text: str


class ResumeRequest(BaseModel):
    extra_budget: float | None = None


class BridgeMessage(BaseModel):
    sender_id: str
    text: str = ""
    message_id: str | None = None
    reply_to_message_id: str | None = None
    attachments: list[dict[str, str]] = []


class BridgeCallback(BaseModel):
    sender_id: str
    data: str
    callback_id: str | None = None


class FireRequest(BaseModel):
    planned_at: str | None = None


def create_app(coord: Coordinator, iface: Any | None = None) -> FastAPI:
    app = FastAPI(title="Open Model Computer Assistant API", version=__version__,
                  description="Proposed controller and worker adapter API. Not the public API of OpenClaw.")
    cfg = coord.config
    if iface is None:
        from .channels.commands import OwnerInterface

        iface = OwnerInterface(coord)

    # -- auth dependencies ------------------------------------------------------------------------------
    def owner(authorization: str | None = Header(default=None)) -> Identity:
        token = _bearer(authorization)
        if _match(token, cfg.server.owner_api_token_ref):
            return Identity("owner_api", "owner")
        raise HTTPException(401, "owner authentication required")

    def bridge(authorization: str | None = Header(default=None)) -> None:
        if not _match(_bearer(authorization), cfg.telegram.openclaw.bridge_token_ref):
            raise HTTPException(401, "bridge authentication required")

    def model_gateway(authorization: str | None = Header(default=None)) -> None:
        if not _match(_bearer(authorization), cfg.server.model_gateway_token_ref):
            raise HTTPException(401, "gateway authentication required")

    def worker(worker_id: str, authorization: str | None = Header(default=None)) -> str:
        if not coord.workers.authenticate(worker_id, _bearer(authorization)):
            raise HTTPException(401, "worker authentication required")
        return worker_id

    def view(task_id: str) -> TaskView:
        t = coord.tasks.get(task_id) or coord.tasks.by_short_id(task_id)
        if t is None:
            raise HTTPException(404, "unknown task")
        return coord.tasks.view(t, coord.artifacts.for_task(t.id, "report") + coord.artifacts.for_task(t.id, "output"))

    # -- health ------------------------------------------------------------------------------------------
    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    # -- tasks -------------------------------------------------------------------------------------------
    @app.post("/tasks", response_model=TaskView, tags=["tasks"], summary="Create a durable task for the authenticated owner")
    def create_task(req: TaskRequest, ident: Identity = Depends(owner)) -> TaskView:
        t = coord.submit_task(req, origin="owner_api")
        return view(t.id)

    @app.get("/tasks", response_model=list[TaskView], tags=["tasks"])
    def list_tasks(limit: int = 20, ident: Identity = Depends(owner)) -> list[TaskView]:
        return [coord.tasks.view(t) for t in coord.tasks.list(limit=min(limit, 200))]

    @app.get("/tasks/{task_id}", response_model=TaskView, tags=["tasks"],
             summary="Retrieve status, artifacts and blocking reason")
    def get_task(task_id: str, ident: Identity = Depends(owner)) -> TaskView:
        return view(task_id)

    @app.post("/tasks/{task_id}/cancel", response_model=TaskView, tags=["tasks"],
              summary="Stop future work and reconcile in-flight actions")
    def cancel_task(task_id: str, ident: Identity = Depends(owner)) -> TaskView:
        t = view(task_id)
        coord.cancel_task(t.task_id)
        return view(t.task_id)

    @app.post("/tasks/{task_id}/pause", response_model=TaskView, tags=["tasks"])
    def pause_task(task_id: str, ident: Identity = Depends(owner)) -> TaskView:
        t = view(task_id)
        coord.pause_task(t.task_id)
        return view(t.task_id)

    @app.post("/tasks/{task_id}/resume", response_model=TaskView, tags=["tasks"])
    def resume_task(task_id: str, body: ResumeRequest = Body(default=ResumeRequest()),
                    ident: Identity = Depends(owner)) -> TaskView:
        t = view(task_id)
        coord.resume_task(t.task_id, extra_budget=body.extra_budget)
        return view(t.task_id)

    @app.post("/tasks/{task_id}/steer", response_model=TaskView, tags=["tasks"])
    def steer_task(task_id: str, body: SteerRequest, ident: Identity = Depends(owner)) -> TaskView:
        t = view(task_id)
        try:
            coord.steer_task(t.task_id, body.text)
        except TransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return view(t.task_id)

    @app.post("/tasks/{task_id}/answer", response_model=TaskView, tags=["tasks"])
    def answer_task(task_id: str, body: SteerRequest, ident: Identity = Depends(owner)) -> TaskView:
        t = view(task_id)
        coord.answer_task(t.task_id, body.text)
        return view(t.task_id)

    @app.get("/artifacts/{artifact_id}", tags=["tasks"], response_class=Response)
    def get_artifact(artifact_id: str, ident: Identity = Depends(owner)) -> Response:
        m = coord.artifacts.get(artifact_id)
        if m is None:
            raise HTTPException(404, "unknown artifact")
        return Response(coord.artifacts.read_bytes(artifact_id), media_type=m.media_type,
                        headers={"X-OMCA-SHA256": m.sha256, "Content-Disposition": f'attachment; filename="{m.name}"'})

    # -- approvals -------------------------------------------------------------------------------------------
    @app.get("/approvals/{approval_id}", response_model=ApprovalRequest, tags=["approvals"])
    def get_approval(approval_id: str, ident: Identity = Depends(owner)) -> ApprovalRequest:
        ap = coord.approvals.get(approval_id)
        if ap is None:
            raise HTTPException(404, "unknown approval")
        return ap

    @app.post("/approvals/{approval_id}/decisions", tags=["approvals"],
              summary="Submit an authenticated action-bound decision")
    def decide(approval_id: str, body: ApprovalDecision, ident: Identity = Depends(owner)) -> dict[str, Any]:
        msg = coord.decide_approval(approval_id, ident, body.decision)
        ap = coord.approvals.get(approval_id)
        return {"message": msg, "status": ap.status.value if ap else None}

    # -- budget / schedules --------------------------------------------------------------------------------------
    @app.get("/budget", response_model=BudgetStatus, tags=["budget"],
             summary="Inspect period, reservations, actual usage and remaining capacity")
    def budget(ident: Identity = Depends(owner)) -> BudgetStatus:
        return coord.ledger.status()

    @app.get("/schedules", tags=["schedules"])
    def schedules(ident: Identity = Depends(owner)) -> list[dict[str, Any]]:
        return coord.scheduler.list()

    @app.post("/schedules/{schedule_id}/fire", tags=["schedules"],
              summary="Trigger a schedule (deduplicated by planned time; for OpenClaw automations)")
    def fire(schedule_id: str, body: FireRequest = Body(default=FireRequest()), ident: Identity = Depends(owner)) -> dict:
        try:
            rid = coord.scheduler.fire_external(schedule_id, body.planned_at)
        except KeyError as exc:
            raise HTTPException(404, "unknown schedule") from exc
        return {"run_id": rid, "fired": rid is not None}

    # -- workers ---------------------------------------------------------------------------------------------
    @app.post("/workers/register", tags=["workers"], summary="Register a worker and capability set")
    def register(reg: WorkerRegistration, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not coord.workers.authenticate(reg.worker_id, _bearer(authorization)):
            raise HTTPException(401, "worker authentication required")
        try:
            return coord.workers.register(reg)
        except WorkerError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/workers/{worker_id}/heartbeat", tags=["workers"])
    def heartbeat(hb: WorkerHeartbeat, worker_id: str = Depends(worker)) -> dict[str, Any]:
        try:
            return coord.workers.heartbeat(worker_id, hb)
        except WorkerError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def _claim(worker_id: str, body: ClaimRequest) -> Response:
        try:
            job = await coord.workers.claim_wait(worker_id, body.session_epoch, min(max(body.wait_seconds, 0), 30))
        except WorkerError as exc:
            raise HTTPException(409, str(exc)) from exc
        if job is None:
            return Response(status_code=204)
        return JSONResponse(job.model_dump(mode="json"))

    @app.post("/workers/{worker_id}/jobs", tags=["workers"], response_model=WorkerJob,
              summary="Dispatch the next fenced bounded job to this worker (long poll; 204 when idle)")
    async def jobs(body: ClaimRequest, worker_id: str = Depends(worker)) -> Response:
        return await _claim(worker_id, body)

    @app.post("/workers/{worker_id}/jobs/claim", tags=["workers"], include_in_schema=False)
    async def jobs_claim(body: ClaimRequest, worker_id: str = Depends(worker)) -> Response:
        return await _claim(worker_id, body)

    @app.post("/workers/{worker_id}/receipts", tags=["workers"], summary="Submit durable execution results")
    def receipts(receipt: ActionReceipt, worker_id: str = Depends(worker),
                 x_omca_session_epoch: str = Header(...)) -> dict[str, Any]:
        try:
            return coord.accept_receipt(worker_id, x_omca_session_epoch, receipt)
        except WorkerError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/workers/{worker_id}/artifacts", tags=["workers"], summary="Upload a declared job output")
    async def upload(worker_id: str = Depends(worker), job_id: str = Form(...), output_kind: str = Form(...),
                     meta: str = Form("{}"), file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        try:
            art = coord.accept_upload(worker_id, job_id, output_kind, file.filename or "upload.bin", data,
                                      media_type=file.content_type, meta=json.loads(meta or "{}"))
        except WorkerError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"artifact_id": art}

    @app.get("/workers/{worker_id}/jobs/{job_id}/inputs/{artifact_id}", tags=["workers"], response_class=Response)
    def job_input(job_id: str, artifact_id: str, worker_id: str = Depends(worker)) -> Response:
        try:
            coord.workers.validate_input_download(worker_id, job_id, artifact_id)
        except WorkerError as exc:
            raise HTTPException(403, str(exc)) from exc
        m = coord.artifacts.get(artifact_id)
        return Response(coord.artifacts.read_bytes(artifact_id), media_type=m.media_type if m else "application/octet-stream",
                        headers={"X-OMCA-SHA256": m.sha256 if m else ""})

    # -- OpenClaw bridge -------------------------------------------------------------------------------------
    @app.post("/bridge/openclaw/message", tags=["bridge"], dependencies=[Depends(bridge)],
              summary="Owner message forwarded by the OpenClaw plugin with its verified sender id")
    async def bridge_message(body: BridgeMessage) -> dict[str, Any]:
        import base64

        atts = [(a.get("name", "file"), base64.b64decode(a["data_b64"])) for a in body.attachments if a.get("data_b64")]
        reply = await iface.handle_message(Identity("telegram", body.sender_id), body.text, update_id=body.message_id,
                                           reply_to_message_id=body.reply_to_message_id, attachments=atts)
        return {"reply": reply}

    @app.post("/bridge/openclaw/callback", tags=["bridge"], dependencies=[Depends(bridge)],
              summary="Inline-button callback (namespace omca) forwarded by the OpenClaw plugin")
    async def bridge_callback(body: BridgeCallback) -> dict[str, Any]:
        reply = await iface.handle_callback(Identity("telegram", body.sender_id), body.data, callback_id=body.callback_id)
        return {"reply": reply}

    # -- model gateway ------------------------------------------------------------------------------------------
    @app.get("/v1/models", tags=["model-gateway"], dependencies=[Depends(model_gateway)])
    def models() -> dict[str, Any]:
        return gateway.list_models(coord.router)

    @app.post("/v1/chat/completions", tags=["model-gateway"], dependencies=[Depends(model_gateway)],
              summary="OpenAI-compatible, allowlisted and metered chat completions for framework-owned calls")
    async def chat(request: Request) -> Response:
        body = await request.json()
        try:
            if body.get("stream"):
                gen = gateway.stream_chat_completions(coord.router, body)
                first = await gen.__anext__()

                async def stream():
                    yield first
                    async for chunk in gen:
                        yield chunk

                return StreamingResponse(stream(), media_type="text/event-stream")
            return JSONResponse(await gateway.chat_completions(coord.router, body))
        except gateway.GatewayError as exc:
            return JSONResponse(exc.body(), status_code=exc.status)
        except StopAsyncIteration:
            return JSONResponse({"error": {"message": "empty stream"}}, status_code=502)

    app.state.coord = coord
    return app


def manifest_list(items: list[ArtifactManifest]) -> list[dict[str, Any]]:
    return [m.model_dump() for m in items]
