"""Browser acceptance cases on local fixtures: AT04, AT05, AT06, AT10, AT11 (FR04-FR08, FR11)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omca.contracts import TaskRequest, TaskStatus

pytestmark = pytest.mark.browser

CANVAS_RULE = {"id": "fixture-canvas", "description": "canvas clicks on the local fixture site",
               "operation": "browser.click_point", "origin": "http://127.0.0.1:*", "element_name_pattern": ".*"}


def browse(s, goal: str, url: str, **params):
    return s.coord.submit_task(TaskRequest(goal=goal, workflow="browser_task", params={"url": url, **params}))


@pytest.mark.acceptance("AT04")
async def test_authorized_session_reused_and_unrelated_profile_untouched(make_system, site, brain, tmp_path):
    profile = tmp_path / "profiles" / "owner-authorized"
    unrelated = tmp_path / "profiles" / "unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "Cookies").write_bytes(b"do-not-touch")
    before = sorted((p.name, p.stat().st_mtime_ns) for p in unrelated.rglob("*"))
    s = await make_system(browser={"attachments": [
        {"id": "vm1-test", "worker_id": "vm1", "mode": "persistent_profile", "channel": "chromium", "headless": True,
         "authorized": True, "user_data_dir": str(profile), "allowed_private_origins": ["http://127.0.0.1:*"]},
        {"id": "vm1-other", "worker_id": "vm1", "mode": "persistent_profile", "channel": "chromium", "headless": True,
         "authorized": False, "user_data_dir": str(unrelated)},
    ]})
    for expected in ("New session started", "Signed in as owner-42"):
        brain.browser_script = [{"name": "finish", "arguments": {"success": True, "summary": "checked session"}}]
        brain.verify_markers = [expected]
        t = browse(s, "Open the session page", site.url("/session.html"))
        t = await s.wait(t.id, timeout=60)
        assert t.status == TaskStatus.COMPLETED, t.result
    # cookies persisted across tasks in the authorized profile; the unrelated profile was never opened
    assert profile.exists() and any(profile.rglob("*"))
    assert sorted((p.name, p.stat().st_mtime_ns) for p in unrelated.rglob("*")) == before
    # an unauthorized attachment is refused even if a task asks for it
    brain.browser_script = []
    t = browse(s, "Open the session page", site.url("/session.html"), attachment_id="vm1-other")
    t = await s.wait(t.id, timeout=30)
    assert t.status == TaskStatus.FAILED and t.error_code == "AUTH_REQUIRED"


@pytest.mark.acceptance("AT05")
async def test_structured_navigation_with_waits_and_fresh_refs(make_system, site, brain):
    s = await make_system()
    t = browse(s, "Find the battery capacity of the Acme Phone X", site.url("/nav/index.html"))
    t = await s.wait(t.id, timeout=90)
    assert t.status == TaskStatus.COMPLETED, (t.result, s.coord.tasks.events(t.id)[-6:])
    ev = s.coord.evidence.for_task(t.id)
    assert any(e.excerpt == "Battery capacity: 4,700 mAh" and e.source_url.endswith("/nav/specs.html") for e in ev)
    # the click on "Specifications" used a reference from a post-change observation and was verified
    rows = s.coord.db.query("SELECT receipt_json FROM actions WHERE task_id=? AND operation='browser.click' ORDER BY created_at",
                            (t.id,))
    assert len(rows) == 2
    verification = json.loads(rows[-1]["receipt_json"])["verification"]
    assert verification["checked"] and verification["checks"]["url_contains"] is True


@pytest.mark.acceptance("AT05")
async def test_stale_reference_is_refused(make_system, site, brain):
    """An element reference from an older observation must not be acted on."""
    s = await make_system()
    brain.browser_script = [{"name": "click", "arguments": {"ref_name": "Show details"}},
                            {"name": "finish", "arguments": {"success": False, "summary": "done"}}]
    t = browse(s, "Open details", site.url("/nav/index.html"))
    t = await s.wait(t.id, timeout=60)
    obs_rows = s.coord.db.query("SELECT id FROM observations WHERE task_id=? ORDER BY captured_at", (t.id,))
    first_obs = obs_rows[0]["id"]
    # replay a click against the first observation's ref after the page changed
    from omca.contracts import WorkerJob
    from omca.util import new_id, now_iso
    from omca.workers import job_deadline

    lease = s.coord.workers.acquire_lease(t.id, "vm1", "vm1:ui")
    job = WorkerJob(job_id=new_id("job"), task_id=t.id, action_id=new_id("act"), worker_id="vm1", lease_id=lease.lease_id,
                    resource="vm1:ui", fencing_token=lease.fencing_token, operation="browser.click",
                    arguments={"ref": "e1", "_obs": first_obs}, idempotency_key=new_id("k"), deadline=job_deadline(20),
                    lease_expires_at=lease.expires_at, issued_at=now_iso(), attachment_id="vm1-test")
    receipt = await s.coord.workers.dispatch(job, 20)
    assert receipt.status.value == "FAILED" and receipt.error.code.value == "STALE_ELEMENT_REF"


@pytest.mark.acceptance("AT06")
@pytest.mark.parametrize("dpr", [1.0, 2.0])
async def test_canvas_target_with_resized_screenshot(make_system, site, brain, dpr):
    s = await make_system(
        browser={"attachments": [{"id": "vm1-test", "worker_id": "vm1", "mode": "launch", "channel": "chromium",
                                  "headless": True, "authorized": True, "viewport": [1280, 800], "device_scale_factor": dpr,
                                  "allowed_private_origins": ["http://127.0.0.1:*"]}]},
        policy={"standing_rules": [CANVAS_RULE]})
    t = browse(s, "Click the red target on the canvas", site.url("/canvas.html"))
    t = await s.wait(t.id, timeout=60)
    assert t.status == TaskStatus.COMPLETED, (t.result, s.coord.tasks.events(t.id)[-6:])
    # the model saw a downscaled screenshot: confirm the transform was not identity
    obs = s.coord.observations.latest(t.id)
    assert obs.screenshot_width == int(1280 * dpr)
    click = s.coord.db.one("SELECT arguments_json, category FROM actions WHERE task_id=? AND operation='browser.click_point'", (t.id,))
    assert click["category"] == "UNKNOWN_MUTATION"  # canvas: allowed only by the narrow standing rule
    assert "Target hit" in obs.text


@pytest.mark.acceptance("AT06")
async def test_canvas_click_without_rule_requires_approval(make_system, site, brain):
    s = await make_system()
    t = browse(s, "Click the red target on the canvas", site.url("/canvas.html"))
    ap = await s.pending_approval(t.id)
    assert ap is not None and ap.operation == "browser.click_point"
    s.coord.cancel_task(t.id)
    t = await s.wait(t.id, timeout=30)
    assert t.status == TaskStatus.CANCELLED


async def _form_task(s, brain, site, steps):
    brain.browser_script = steps
    return browse(s, "Use the contact form", site.url("/form.html"))


@pytest.mark.acceptance("AT10")
@pytest.mark.parametrize("step", [
    {"name": "click", "arguments": {"ref_name": "Send message"}},
    {"name": "click", "arguments": {"ref_name": "Delete account"}},
    {"name": "click", "arguments": {"ref_name": "Dark mode"}},          # unknown GUI mutation
    {"name": "press", "arguments": {"key": "e"}},                       # keyboard shortcut outside a text field
    {"name": "type", "arguments": {"ref_name": "Message", "text": "hi", "submit": True}},  # Enter-submit route
])
async def test_consequential_actions_wait_for_a_decision(make_system, site, brain, step):
    s = await make_system()
    start = len(site.submissions)
    t = await _form_task(s, brain, site, [step, {"name": "finish", "arguments": {"success": False, "summary": "stop"}}])
    ap = await s.pending_approval(t.id)
    assert ap is not None, f"{step} executed without an approval request"
    await asyncio.sleep(0.5)
    assert len(site.submissions) == start, "the effect happened before a decision"
    s.coord.decide_approval(ap.approval_id, s.owner(), "deny")
    t = await s.wait(t.id, timeout=60)
    assert len(site.submissions) == start
    act = s.coord.db.one("SELECT status FROM actions WHERE approval_id=?", (ap.approval_id,))
    assert act["status"] == "DENIED"


@pytest.mark.acceptance("AT10")
async def test_approved_send_executes_exactly_once(make_system, site, brain):
    s = await make_system()
    start = len(site.submissions)
    t = await _form_task(s, brain, site, [
        {"name": "type", "arguments": {"ref_name": "Message", "text": "hello owner"}},
        {"name": "click", "arguments": {"ref_name": "Send message", "expect_text": "Message sent"}},
        {"name": "finish", "arguments": {"success": True, "summary": "sent"}},
    ])
    ap = await s.pending_approval(t.id)
    assert ap.destination and ap.destination.startswith(site.base)
    assert "Send message" in ap.preview
    s.coord.decide_approval(ap.approval_id, s.owner(), "approve")
    t = await s.wait(t.id, timeout=60)
    assert t.status == TaskStatus.COMPLETED, t.result
    sent = site.submissions[start:]
    assert len(sent) == 1 and "msg=hello+owner" in sent[0]["body"]
    assert s.coord.approvals.get(ap.approval_id).status.value == "CONSUMED"


@pytest.mark.acceptance("AT11")
async def test_changed_target_state_invalidates_approval(make_system, site, brain):
    s = await make_system()
    start = len(site.submissions)
    t = await _form_task(s, brain, site, [{"name": "click", "arguments": {"ref_name": "Send message"}},
                                          {"name": "finish", "arguments": {"success": False, "summary": "stop"}}])
    ap = await s.pending_approval(t.id)
    page = s.worker.browser._sessions["vm1-test"].pages[t.id]
    await page.evaluate("document.querySelector('button[type=submit]').textContent = 'Send payment'")
    s.coord.decide_approval(ap.approval_id, s.owner(), "approve")
    t = await s.wait(t.id, timeout=60)
    assert len(site.submissions) == start
    assert s.coord.approvals.get(ap.approval_id).status.value == "INVALIDATED"
    act = s.coord.db.one("SELECT status, receipt_json FROM actions WHERE approval_id=?", (ap.approval_id,))
    assert act["status"] == "DENIED" and "target state changed" in act["receipt_json"]


@pytest.mark.acceptance("AT11")
async def test_expired_approval_and_wrong_owner_are_refused(make_system, site, brain):
    s = await make_system(limits={"approval_expiry_minutes": 0.02, "progress_interval_seconds": 3600})
    start = len(site.submissions)
    t = await _form_task(s, brain, site, [{"name": "click", "arguments": {"ref_name": "Send message"}},
                                          {"name": "finish", "arguments": {"success": False, "summary": "stop"}}])
    ap = await s.pending_approval(t.id)
    from omca.policy import Identity

    msg = s.coord.decide_approval(ap.approval_id, Identity("telegram", "999999"), "approve")
    assert msg.startswith("Not accepted")
    await asyncio.sleep(1.5)
    msg = s.coord.decide_approval(ap.approval_id, s.owner(), "approve")
    assert msg.startswith("Not accepted") and "expire" in msg
    t = await s.wait(t.id, timeout=60)
    assert len(site.submissions) == start
    assert s.coord.approvals.get(ap.approval_id).status.value == "EXPIRED"


def test_fixture_site_files_present():
    assert (Path(__file__).parent / "fixtures" / "site" / "canvas.html").exists()
