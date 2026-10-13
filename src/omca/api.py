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

import asyncio
import hmac
import json
import re
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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


class UiCallback(BaseModel):
    data: str


UI_IDENTITY = Identity("owner_api", "web-ui")
UI_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UI_ASSETS = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
UI_CONVERSATION_ID = re.compile(r"cnv_[A-Za-z0-9]{6,40}")
UI_TASK_REF =re.compile(r"^/(?:omca\s+)?(?:steer|answer|status|pause|resume|cancel)\s+(\S+)", re.I)


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

    # -- local web UI ----------------------------------------------------------------------------------------
    if coord.web_inbox is not None:
        inbox = coord.web_inbox

        @app.get("/ui", tags=["ui"], response_class=HTMLResponse, include_in_schema=False)
        def ui_page() -> HTMLResponse:
            from importlib import resources

            return HTMLResponse((resources.files("omca.web") / "index.html").read_text(encoding="utf-8"),
                                headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                         "Referrer-Policy": "no-referrer"})

        @app.get("/ui/assets/{name}", tags=["ui"], include_in_schema=False)
        def ui_asset(name: str) -> Response:
            from importlib import resources

            if name not in UI_ASSETS:
                raise HTTPException(404, "unknown asset")
            return Response((resources.files("omca.web") / name).read_bytes(), media_type=UI_ASSETS[name],
                            headers={"Cache-Control": "no-cache"})

        @app.get("/ui/api/messages", tags=["ui"], summary="Conversation changes since a revision (long poll)")
        async def ui_messages(after: int = 0, wait: float = 0, ident: Identity = Depends(owner)) -> dict[str, Any]:
            deadline = asyncio.get_running_loop().time() + max(0.0, min(wait, 25.0))
            while True:
                items = inbox.feed(after)
                if items or asyncio.get_running_loop().time() >= deadline:
                    return {"messages": items, "rev": items[-1]["rev"] if items else after}
                await asyncio.sleep(0.4)

        @app.get("/ui/api/info", tags=["ui"], summary="Assistant configuration summary for the web UI")
        def ui_info(ident: Identity = Depends(owner)) -> dict[str, Any]:
            roles = {}
            for role in ("planning", "vision", "summarisation", "review"):
                rid = getattr(cfg.models.roles, role)
                r = cfg.models.route(rid) if rid else None
                roles[role] = {"route": rid, "model": r.model if r else None}
            return {"version": __version__, "telegram_mode": cfg.telegram.mode,
                    "telegram_connected": coord.notifier.channel is not None and bool(coord.notifier.owner_ids()),
                    "browser_engine": cfg.browser.engine, "browser_approval": cfg.browser.agent.require_approval,
                    "browser_authorized": any(a.authorized for a in cfg.browser.attachments),
                    "search_adapter": cfg.search.adapter, "roles": roles, "timezone": cfg.owner.display_timezone,
                    "currency": cfg.budget.currency, "per_task_ceiling": cfg.budget.per_task_reservation_ceiling}

        @app.get("/ui/api/tasks/{task_id}", tags=["ui"], summary="Task details with model spend")
        def ui_task(task_id: str, ident: Identity = Depends(owner)) -> dict[str, Any]:
            t = view(task_id)
            return {**t.model_dump(mode="json"), "spend": coord.ledger.task_spend(t.task_id),
                    "currency": cfg.budget.currency}

        @app.post("/ui/api/messages", tags=["ui"], summary="Send an owner message or command from the web UI")
        async def ui_send(text: str = Form(default=""), conversation_id: str | None = Form(default=None),
                          files: list[UploadFile] = File(default=[]),
                          ident: Identity = Depends(owner)) -> dict[str, Any]:
            from .util import new_id

            if conversation_id is not None and not UI_CONVERSATION_ID.fullmatch(conversation_id):
                raise HTTPException(400, "invalid conversation id")

            attachments = []
            for f in files[:10]:
                data = await f.read(UI_MAX_UPLOAD_BYTES + 1)
                if len(data) > UI_MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"{f.filename} is larger than 20 MB")
                attachments.append((f.filename or "file", data))
            if not text.strip() and not attachments:
                raise HTTPException(400, "empty message")
            names = ", ".join(n for n, _ in attachments)
            mid = inbox.add("owner", text.strip() + (f"\n[attached: {names}]" if names else ""),
                            conversation_id=conversation_id)
            newest = coord.tasks.list(limit=1)
            before = newest[0].id if newest else None
            reply = await iface.handle_message(UI_IDENTITY, text, update_id=new_id("web"), attachments=attachments,
                                            conversation_id=conversation_id)
            # thread the message: the task it created, or the task a command addressed
            task_id = None
            created = coord.tasks.list(limit=1)
            if created and created[0].id != before:
                task_id = created[0].id
            elif (m := UI_TASK_REF.match(text.strip())) and (t := coord.tasks.by_short_id(m.group(1))):
                task_id = t.id
            if task_id:
                inbox.link_task(mid, task_id)
            if reply:
                inbox.add("assistant", reply, task_id=task_id, conversation_id=conversation_id)
            await coord.notifier.flush()
            return {"reply": reply, "task_id": task_id, "conversation_id": conversation_id}

        @app.post("/ui/api/callback", tags=["ui"], summary="Press an inline button (approvals) from the web UI")
        async def ui_callback(body: UiCallback, ident: Identity = Depends(owner)) -> dict[str, Any]:
            from .util import new_id

            reply = await iface.handle_callback(UI_IDENTITY, body.data, callback_id=new_id("webcb"))
            await coord.notifier.flush()
            return {"reply": reply}

        @app.get("/ui/api/files/{message_id}", tags=["ui"], response_class=Response)
        def ui_file(message_id: int, ident: Identity = Depends(owner)) -> Response:
            doc = inbox.document(message_id)
            if doc is None:
                raise HTTPException(404, "unknown document")
            name, data = doc
            safe = name.replace('"', "").replace("\r", "").replace("\n", "")
            return Response(data, media_type="application/octet-stream",
                            headers={"Content-Disposition": f'attachment; filename="{safe}"'})

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
