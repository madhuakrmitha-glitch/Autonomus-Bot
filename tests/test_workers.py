"""Workers: AT22 (collision), AT23 (worker loss / fencing), AT25 (artifact transfer) and FR22 routing."""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from omca.api import create_app
from omca.config import config_from_dict
from omca.contracts import (
    ActionReceipt,
    ReceiptStatus,
    TaskRequest,
    TaskStatus,
    WorkerHeartbeat,
    WorkerJob,
    WorkerRegistration,
)
from omca.coordinator import Coordinator
from omca.util import new_id, now_iso
from omca.worker.agent import HandlerResult, InProcessTransport, WorkerAgent
from omca.workers import WorkerError, job_deadline

from .conftest import base_config


def two_worker_cfg(tmp_path, site=None, **extra):
    os.environ["OMCA_TEST_WORKER_VM1"] = "secret-vm1"
    os.environ["OMCA_TEST_WORKER_VM2"] = "secret-vm2"
    os.environ["OMCA_TEST_OWNER_TOKEN"] = "owner-token"
    return config_from_dict(base_config(
        tmp_path, site,
        workers=[{"id": "vm1", "credential_ref": "env:OMCA_TEST_WORKER_VM1", "capabilities": ["browser", "spreadsheet"]},
                 {"id": "vm2", "credential_ref": "env:OMCA_TEST_WORKER_VM2", "capabilities": ["spreadsheet", "video"]}],
        browser={"attachments": []}, server={"owner_api_token_ref": "env:OMCA_TEST_OWNER_TOKEN"}, **extra))


def reg(worker_id: str, epoch: str, caps: list[str]) -> WorkerRegistration:
    return WorkerRegistration(worker_id=worker_id, capabilities=caps, session_epoch=epoch, version="test")


# ---------------------------------------------------------------------------------------------------------
# AT22 VM collision
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.acceptance("AT22")
def test_ui_lease_is_exclusive_and_fencing_tokens_increase(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    a = c.workers.acquire_lease("tsk_A", "vm1", "vm1:ui")
    with pytest.raises(WorkerError):
        c.workers.acquire_lease("tsk_B", "vm1", "vm1:ui")
    # the same task renews its own lease without a new token
    assert c.workers.acquire_lease("tsk_A", "vm1", "vm1:ui").fencing_token == a.fencing_token
    c.workers.release_lease(a.lease_id)
    b = c.workers.acquire_lease("tsk_B", "vm1", "vm1:ui")
    assert b.fencing_token == a.fencing_token + 1
    # leases on another VM are independent
    assert c.workers.acquire_lease("tsk_C", "vm2", "vm2:ui").fencing_token == 1


@pytest.mark.acceptance("AT22")
@pytest.mark.browser
async def test_two_browser_tasks_never_interleave_on_one_desktop(make_system, site, brain):
    s = await make_system()
    script = [{"name": "wait", "arguments": {"ms": 300}} for _ in range(3)] + [
        {"name": "finish", "arguments": {"success": True, "summary": "done"}}]
    brain.browser_script = script * 2
    brain.verify_markers = ["Contact us"]
    t1 = s.coord.submit_task(TaskRequest(goal="first", workflow="browser_task", params={"url": site.url("/form.html")}))
    t2 = s.coord.submit_task(TaskRequest(goal="second", workflow="browser_task", params={"url": site.url("/form.html")}))
    t1 = await s.wait(t1.id, timeout=90)
    t2 = await s.wait(t2.id, timeout=90)
    assert {t1.status, t2.status} == {TaskStatus.COMPLETED}
    spans = {}
    for tid in (t1.id, t2.id):
        rows = s.coord.db.query("SELECT receipt_json FROM actions WHERE task_id=? AND operation LIKE 'browser.%' "
                                "AND receipt_json IS NOT NULL", (tid,))
        times = [(r["receipt_json"].split('"started_at":"')[1][:24], r["receipt_json"].split('"finished_at":"')[1][:24])
                 for r in rows if '"started_at":"' in r["receipt_json"]]
        spans[tid] = (min(a for a, _ in times), max(b for _, b in times))
    (a0, a1), (b0, b1) = spans[t1.id], spans[t2.id]
    assert a1 <= b0 or b1 <= a0, f"desktop use overlapped: {spans}"
    # the second task was held back by the lease, not run concurrently
    blocked = [e for tid in (t1.id, t2.id) for e in s.coord.tasks.events(tid) if e["kind"] == "status" and e["to"] == "BLOCKED"]
    assert blocked and "leased to task" in blocked[0]["reason"]


# ---------------------------------------------------------------------------------------------------------
# AT23 worker loss
# ---------------------------------------------------------------------------------------------------------


def _job(c, task_id: str, worker: str, op: str = "spreadsheet.read", resource: str | None = None, **kw) -> WorkerJob:
    resource = resource or f"{worker}:ui"
    lease = c.workers.acquire_lease(task_id, worker, resource)
    return WorkerJob(job_id=new_id("job"), task_id=task_id, action_id=new_id("act"), worker_id=worker, lease_id=lease.lease_id,
                     resource=resource, fencing_token=lease.fencing_token, operation=op, arguments=kw.get("args", {}),
                     idempotency_key=new_id("k"), deadline=job_deadline(30), lease_expires_at=lease.expires_at,
                     issued_at=now_iso())


@pytest.mark.acceptance("AT23")
async def test_stale_worker_cannot_act_and_late_receipts_are_flagged(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    effects = []

    async def effect(job, ctx):
        effects.append(job.job_id)
        return HandlerResult(result={"ok": True})

    old = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w-old", capabilities=["spreadsheet"],
                      handlers={"spreadsheet.read": effect})
    await old.start()
    job = c.workers.create_job(_job(c, "tsk_A", "vm1"))
    claimed = c.workers.claim("vm1", old.epoch)
    assert claimed and claimed.job_id == job.job_id

    # the worker process restarts (new epoch) before the old process reports: the old job becomes uncertain
    new = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w-new", capabilities=["spreadsheet"],
                      handlers={"spreadsheet.read": effect})
    await new.start()
    stored = c.workers.receipt_for_action(job.action_id)
    assert stored.status == ReceiptStatus.UNKNOWN and stored.error.code.value == "UNCERTAIN_SIDE_EFFECT"
    # the stale process can no longer claim work or heartbeat
    with pytest.raises(WorkerError):
        c.workers.claim("vm1", old.epoch)
    with pytest.raises(WorkerError):
        c.workers.heartbeat("vm1", WorkerHeartbeat(session_epoch=old.epoch))
    # a late receipt from the stale process is recorded for reconciliation but not accepted
    late = ActionReceipt(action_id=job.action_id, job_id=job.job_id, worker_id="vm1", fencing_token=job.fencing_token,
                         status=ReceiptStatus.SUCCEEDED, started_at=now_iso(), finished_at=now_iso())
    assert c.workers.submit_receipt("vm1", old.epoch, late) == {"accepted": False, "stale": True}
    assert c.db.scalar("SELECT COUNT(*) FROM task_events WHERE kind='stale_receipt'") == 1

    # a newer lease fences out jobs issued under the old token, on both sides
    c.workers.release_task_leases("tsk_A")
    stale_job = _job(c, "tsk_B", "vm1")
    c.workers.release_task_leases("tsk_B")
    c.workers.acquire_lease("tsk_C", "vm1", "vm1:ui")
    c.workers.create_job(stale_job)
    assert c.workers.claim("vm1", new.epoch) is None  # refused at claim: lease superseded
    assert c.workers.receipt_for_action(stale_job.action_id).error.code.value == "STALE_FENCING_TOKEN"
    new.highest_tokens["vm1:ui"] = stale_job.fencing_token + 1
    receipt = await new.execute(stale_job)
    assert receipt.status == ReceiptStatus.REJECTED and receipt.error.code.value == "STALE_FENCING_TOKEN"
    assert effects == []  # nothing executed under a stale token


@pytest.mark.acceptance("AT23")
async def test_worker_that_lost_contact_stops_acting(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    ran = []

    async def effect(job, ctx):
        ran.append(job.job_id)
        return HandlerResult()

    w = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w", capabilities=["spreadsheet"],
                    handlers={"spreadsheet.read": effect})
    await w.start()
    job = _job(c, "tsk_A", "vm1")
    w.last_heartbeat_ok -= 3600  # no successful heartbeat for an hour
    receipt = await w.execute(job)
    assert receipt.status == ReceiptStatus.REJECTED and receipt.error.code.value == "WORKER_UNAVAILABLE"
    assert ran == []


@pytest.mark.acceptance("AT23")
async def test_worker_receipt_cache_makes_execution_idempotent(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    ran = []

    async def effect(job, ctx):
        ran.append(job.job_id)
        return HandlerResult(result={"n": len(ran)})

    w = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w", capabilities=["spreadsheet"],
                    handlers={"spreadsheet.read": effect})
    await w.start()
    job = _job(c, "tsk_A", "vm1")
    r1 = await w.execute(job)
    r2 = await w.execute(job)  # duplicate delivery of the same action
    assert r1.result == r2.result == {"n": 1} and len(ran) == 1


# ---------------------------------------------------------------------------------------------------------
# FR22 routing and AT25 artifact transfer
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.acceptance("AT25")
def test_routing_and_declared_artifact_transfer_over_http(tmp_path):
    cfg = two_worker_cfg(tmp_path)
    c = Coordinator(cfg)
    client = TestClient(create_app(c))
    h1 = {"Authorization": "Bearer secret-vm1"}
    h2 = {"Authorization": "Bearer secret-vm2"}
    # authentication: a worker may only register as itself
    assert client.post("/workers/register", json=reg("vm2", "e", ["spreadsheet"]).model_dump(), headers=h1).status_code == 401
    assert client.post("/workers/register", json=reg("vm1", "e1", ["browser", "spreadsheet"]).model_dump(), headers=h1).status_code == 200
    assert client.post("/workers/register", json=reg("vm2", "e2", ["spreadsheet", "video"]).model_dump(), headers=h2).status_code == 200
    # capability routing (FR22): video only exists on vm2; explicit routing is honoured
    assert c.workers.route("video") == "vm2"
    assert c.workers.route("spreadsheet", preferred="vm1") == "vm1"
    with pytest.raises(WorkerError):
        c.workers.route("browser", preferred="vm2")

    task = c.tasks.create(TaskRequest(goal="x"), owner=c.owner_key, workflow="desktop_workflow", limits={})
    # vm1 produces a declared output
    j1 = c.workers.create_job(_job(c, task.id, "vm1", op="spreadsheet.write", resource=f"vm1:task:{task.id}"))
    claim = client.post("/workers/vm1/jobs", json={"session_epoch": "e1", "wait_seconds": 0}, headers=h1)
    assert claim.status_code == 200 and claim.json()["job_id"] == j1.job_id
    data = b"PK\x03\x04" + b"0" * 64
    from io import BytesIO

    from openpyxl import Workbook

    buf = BytesIO()
    Workbook().save(buf)
    data = buf.getvalue()
    good = hashlib.sha256(data).hexdigest()

    def upload(kind, meta, headers=h1, worker="vm1", job=j1.job_id, payload=data):
        return client.post(f"/workers/{worker}/artifacts", headers=headers,
                           data={"job_id": job, "output_kind": kind, "meta": __import__("json").dumps(meta)},
                           files={"file": ("out.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})

    assert upload("screenshot", {"sha256": good}).status_code == 403            # undeclared output kind
    assert upload("workbook", {"sha256": "0" * 64}).status_code == 403           # incorrect hash
    assert upload("workbook", {"sha256": good, "destination": "../../etc"}).status_code == 403  # bad destination
    assert upload("workbook", {"sha256": good}, headers=h2, worker="vm2").status_code == 403  # not vm2's job
    ok = upload("workbook", {"sha256": good})
    assert ok.status_code == 200
    art = ok.json()["artifact_id"]
    assert c.artifacts.get(art).sha256 == good and c.artifacts.get(art).rel_path.startswith(f"tasks/{task.id}/")

    # vm2 receives it only as a declared input of its own job, with a verifiable hash
    j2 = _job(c, task.id, "vm2", op="spreadsheet.read", resource=f"vm2:task:{task.id}")
    j2 = c.workers.create_job(j2.model_copy(update={"declared_inputs": [art]}))
    other = c.artifacts.put(task.id, "input", "secret.txt", b"not declared").manifest.artifact_id
    assert client.post("/workers/vm2/jobs", json={"session_epoch": "e2", "wait_seconds": 0}, headers=h2).status_code == 200
    got = client.get(f"/workers/vm2/jobs/{j2.job_id}/inputs/{art}", headers=h2)
    assert got.status_code == 200 and hashlib.sha256(got.content).hexdigest() == got.headers["X-OMCA-SHA256"] == good
    assert client.get(f"/workers/vm2/jobs/{j2.job_id}/inputs/{other}", headers=h2).status_code == 403
    assert client.get(f"/workers/vm1/jobs/{j2.job_id}/inputs/{art}", headers=h1).status_code == 403
    assert client.get(f"/workers/vm2/jobs/{j2.job_id}/inputs/{art}", headers=h1).status_code == 401


def test_heartbeat_marks_offline_and_routing_skips_dead_workers(tmp_path):
    cfg = two_worker_cfg(tmp_path, limits={"lease_ttl_seconds": 1, "progress_interval_seconds": 3600})
    c = Coordinator(cfg)
    c.workers.register(reg("vm2", "e2", ["spreadsheet", "video"]))
    assert c.workers.route("video") == "vm2"
    c.db.execute("UPDATE workers SET last_heartbeat='2000-01-01T00:00:00.000Z' WHERE id='vm2'")
    with pytest.raises(WorkerError):
        c.workers.route("video")


def test_receipt_wait_times_out_to_unknown_only_after_a_claim(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    c.workers.register(reg("vm1", "e1", ["spreadsheet"]))

    async def go():
        unclaimed = await c.workers.dispatch(_job(c, "tsk_A", "vm1", resource="vm1:task:A"), 0.3)
        assert unclaimed.status == ReceiptStatus.FAILED and unclaimed.error.code.value == "WORKER_UNAVAILABLE"
        job = _job(c, "tsk_B", "vm1", resource="vm1:task:B")
        fut = asyncio.ensure_future(c.workers.dispatch(job, 0.5))
        await asyncio.sleep(0.05)
        assert c.workers.claim("vm1", "e1") is not None
        claimed = await fut
        assert claimed.status == ReceiptStatus.UNKNOWN

    asyncio.run(go())
