"""Regression tests for the safety review: each test pins one fixed failure mode."""

from __future__ import annotations

import asyncio

import pytest

from omca import netguard
from omca.contracts import ActionStatus, ReceiptStatus, TaskRequest, TaskStatus, WorkerHeartbeat
from omca.coordinator import Coordinator
from omca.db import dumps
from omca.engine import TaskContext
from omca.netguard import is_private_host, url_problem
from omca.util import new_id, now_iso
from omca.worker.agent import HandlerResult, InProcessTransport, WorkerAgent
from omca.workers import WorkerError

from .test_workers import _job, two_worker_cfg

# ---------------------------------------------------------------------------------------------------------
# Workers: fencing, cancellation, re-execution
# ---------------------------------------------------------------------------------------------------------


async def test_superseded_worker_process_fences_itself(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    ran = []

    async def effect(job, ctx):
        ran.append(job.job_id)
        return HandlerResult()

    old = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "old", capabilities=["spreadsheet"],
                      handlers={"spreadsheet.write": effect})
    await old.start()
    new = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "new", capabilities=["spreadsheet"],
                      handlers={"spreadsheet.write": effect})
    await new.start()
    await old.heartbeat()  # the coordinator refuses the old session epoch
    assert old.fenced
    receipt = await old.execute(_job(c, "tsk_A", "vm1", op="spreadsheet.write"))
    assert receipt.status == ReceiptStatus.REJECTED and receipt.error.code.value == "STALE_FENCING_TOKEN"
    assert ran == []
    # its claim loop ends instead of retrying forever
    await asyncio.wait_for(old.run_loop_registered(), 5)
    with pytest.raises(WorkerError):
        c.workers.claim("vm1", old.epoch)


async def test_cancel_interrupts_claimed_read_only_jobs_but_lets_mutations_finish(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    started = asyncio.Event()

    async def slow_read(job, ctx):
        started.set()
        await asyncio.sleep(3600)

    w = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w", capabilities=["spreadsheet"],
                    handlers={"spreadsheet.read": slow_read})
    await w.start()
    read = c.workers.create_job(_job(c, "tsk_A", "vm1", resource="vm1:task:A"))
    write = c.workers.create_job(_job(c, "tsk_B", "vm1", op="spreadsheet.write", resource="vm1:task:B"))
    assert {c.workers.claim("vm1", w.epoch).job_id for _ in range(2)} == {read.job_id, write.job_id}
    c.workers.cancel_job(read.job_id)
    c.workers.cancel_job(write.job_id)
    hb = c.workers.heartbeat("vm1", WorkerHeartbeat(session_epoch=w.epoch))
    assert hb["cancel_job_ids"] == [read.job_id]

    running = asyncio.ensure_future(w.execute(read))
    await asyncio.wait_for(started.wait(), 5)
    await w.heartbeat()  # delivers the cancellation to the running job
    receipt = await asyncio.wait_for(running, 5)
    assert receipt.status == ReceiptStatus.UNKNOWN and receipt.error.code.value == "CANCELLED"


async def test_read_only_actions_run_again_under_a_new_job_but_effects_never_repeat(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    ran: list[str] = []

    async def handler(job, ctx):
        ran.append(job.operation)
        return HandlerResult(result={"n": len(ran)})

    w = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w", capabilities=["spreadsheet"],
                    handlers={"spreadsheet.read": handler, "spreadsheet.write": handler})
    await w.start()
    for op, expected_runs in (("spreadsheet.read", 2), ("spreadsheet.write", 1)):
        ran.clear()
        first = c.workers.create_job(_job(c, f"tsk_{op}", "vm1", op=op, resource=f"vm1:task:{op}"))
        assert c.workers.claim("vm1", w.epoch).job_id == first.job_id
        r1 = await w.execute(first)
        c.workers.submit_receipt("vm1", w.epoch, r1)
        c.workers.supersede_job(first.job_id)
        again = c.workers.create_job(first.model_copy(update={"job_id": new_id("job")}))
        assert again.job_id != first.job_id, "a finished job must not block a new attempt"
        assert c.workers.claim("vm1", w.epoch).job_id == again.job_id
        r2 = await w.execute(again)
        assert r2.job_id == again.job_id
        assert c.workers.submit_receipt("vm1", w.epoch, r2) == {"accepted": True}
        assert len(ran) == expected_runs, op
        assert c.workers.receipt_for_action(first.action_id).job_id == again.job_id


# ---------------------------------------------------------------------------------------------------------
# Engine: uncertain effects are reconciled, never re-run
# ---------------------------------------------------------------------------------------------------------


def _ctx(c: Coordinator) -> TaskContext:
    task = c.tasks.create(TaskRequest(goal="x"), owner=c.owner_key, workflow="desktop_workflow", limits={})
    return TaskContext(c, task, asyncio.Event())


def _action_row(ctx: TaskContext, key: str, operation: str, status: ActionStatus, arguments: dict) -> str:
    action_id = new_id("act")
    now = now_iso()
    ctx.c.db.execute(
        """INSERT INTO actions(id, task_id, step_key, operation, target_json, arguments_json, arguments_hash,
           idempotency_key, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (action_id, ctx.task.id, key, operation, dumps({}), dumps(arguments), "h", f"{ctx.task.id}:{key}",
         status.value, now, now))
    return action_id


@pytest.mark.parametrize("status", [ActionStatus.UNKNOWN, ActionStatus.DISPATCHED])
async def test_uncertain_coordinator_effect_is_reconciled_with_the_owner(tmp_path, status):
    c = Coordinator(two_worker_cfg(tmp_path, policy={"contacts": [{"name": "A", "kind": "telegram", "address": "@asha"}]}))
    ctx = _ctx(c)
    args = {"recipient": "@asha", "text": "hello"}
    action_id = _action_row(ctx, "send", "message.send", status, args)
    executed, asked = [], []

    async def execute_local(op, a, _ctx):
        executed.append(op)
        return {}

    async def ask_owner(question, *, key, timeout_hours=None):
        asked.append(question)
        return "done"

    c.execute_local = execute_local
    ctx.ask_owner = ask_owner
    out = await asyncio.wait_for(ctx.act("message.send", args, key="send"), 10)
    assert executed == [] and len(asked) == 1
    assert out.status == ActionStatus.SUCCEEDED and out.action_id == action_id


async def test_never_started_is_trusted_only_from_the_claiming_process(tmp_path):
    c = Coordinator(two_worker_cfg(tmp_path))
    ctx = _ctx(c)
    w1 = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w1", capabilities=["spreadsheet"])
    await w1.start()
    original = c.workers.create_job(_job(c, ctx.task.id, "vm1", op="spreadsheet.write", resource="vm1:task:o"))
    c.workers.claim("vm1", w1.epoch)
    probe_same = c.workers.create_job(_job(c, ctx.task.id, "vm1", resource="vm1:task:p1"))
    c.workers.claim("vm1", w1.epoch)
    w2 = WorkerAgent("vm1", InProcessTransport(c, "vm1"), workspace=tmp_path / "w2", capabilities=["spreadsheet"])
    await w2.start()
    probe_new = c.workers.create_job(_job(c, ctx.task.id, "vm1", resource="vm1:task:p2"))
    c.workers.claim("vm1", w2.epoch)

    job = c.workers.job_for_action(original.action_id)

    class Rec:
        def __init__(self, action_id):
            self.action_id = action_id

    assert ctx._same_session(job, Rec(probe_same.action_id))
    assert not ctx._same_session(job, Rec(probe_new.action_id))


# ---------------------------------------------------------------------------------------------------------
# Browser: approvals are re-checked against the live target
# ---------------------------------------------------------------------------------------------------------


def _browse(s, brain, site, steps):
    brain.browser_script = steps
    return s.coord.submit_task(TaskRequest(goal="Use the contact form", workflow="browser_task",
                                           params={"url": site.url("/form.html")}))


@pytest.mark.browser
async def test_approved_key_press_is_refused_when_focus_moved(make_system, site, brain):
    s = await make_system()
    start = len(site.submissions)
    t = _browse(s, brain, site, [
        {"name": "type", "arguments": {"ref_name": "Message", "text": "hi"}},
        {"name": "press", "arguments": {"key": "Enter"}},
        {"name": "finish", "arguments": {"success": False, "summary": "stop"}},
    ])
    ap = await s.pending_approval(t.id)
    assert ap is not None and ap.preconditions["name"] == "Message"
    page = s.worker.browser._sessions["vm1-test"].pages[t.id]
    await page.evaluate("document.getElementById('del').focus()")
    s.coord.decide_approval(ap.approval_id, s.owner(), "approve")
    await s.wait(t.id, timeout=60)
    assert len(site.submissions) == start
    assert s.coord.approvals.get(ap.approval_id).status.value == "INVALIDATED"


@pytest.mark.browser
async def test_approval_is_bound_to_the_page_url(make_system, site, brain):
    s = await make_system()
    start = len(site.submissions)
    t = _browse(s, brain, site, [{"name": "click", "arguments": {"ref_name": "Send message"}},
                                 {"name": "finish", "arguments": {"success": False, "summary": "stop"}}])
    ap = await s.pending_approval(t.id)
    assert ap.preconditions["url"] == site.url("/form.html")
    page = s.worker.browser._sessions["vm1-test"].pages[t.id]
    # same document and button, different address: the approval no longer describes this page
    await page.evaluate("history.pushState({}, '', '/form.html?to=someone-else')")
    s.coord.decide_approval(ap.approval_id, s.owner(), "approve")
    await s.wait(t.id, timeout=60)
    assert len(site.submissions) == start
    assert s.coord.approvals.get(ap.approval_id).status.value == "INVALIDATED"


@pytest.mark.browser
async def test_worker_refuses_a_press_or_point_click_on_a_different_target(make_system, site, brain):
    s = await make_system()
    task = s.coord.tasks.create(TaskRequest(goal="probe"), owner=s.coord.owner_key, workflow="browser_task", limits={})
    s.coord.tasks.transition(task.id, TaskStatus.CANCELLED, reason="jobs are dispatched by hand")  # keep the engine off it

    async def run(op, args):
        job = _job(s.coord, task.id, "vm1", op=op, resource="vm1:ui", args=args)
        return await s.coord.workers.dispatch(job.model_copy(update={"attachment_id": "vm1-test"}), 30)

    await run("browser.navigate", {"url": site.url("/form.html")})
    moved = await run("browser.press", {"key": "Enter", "_focus_fp": "0123456789abcdef"})
    assert moved.status == ReceiptStatus.FAILED and moved.error.code.value == "STALE_ELEMENT_REF"
    nothing_focused = await run("browser.press", {"key": "PageDown", "_focus_fp": "none"})
    assert nothing_focused.status == ReceiptStatus.SUCCEEDED
    other = await run("browser.click_point", {"x": 5, "y": 5, "_fingerprint": "0123456789abcdef"})
    assert other.status == ReceiptStatus.FAILED and other.error.code.value == "STALE_ELEMENT_REF"


# ---------------------------------------------------------------------------------------------------------
# Network destinations (SSRF)
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.1", "0x7f.0.0.1", "2130706433", "017700000001", "::ffff:127.0.0.1", "::1",
                                  "0.0.0.0", "localhost.", "printer.localhost", "169.254.169.254", "100.64.0.1",
                                  "[fd00::1]", "router", "nas.home.arpa"])
def test_private_hosts_in_any_spelling(host):
    assert is_private_host(host)


@pytest.mark.parametrize("host", ["example.com", "8.8.8.8", "2606:4700:4700::1111", "cafe.be"])
def test_public_hosts(host):
    assert not is_private_host(host)


@pytest.mark.parametrize("url", ["http://evil.example\\@127.0.0.1/", "http://user:pw@example.com/",
                                 "http://exa mple.com/", "http://example.com:99999/", "ftp://example.com/"])
def test_ambiguous_urls_are_refused(url):
    assert url_problem(url)


def test_navigation_classifier_uses_the_same_host_rules(tmp_path):
    from omca.config import config_from_dict
    from omca.contracts import ActionIntent
    from omca.db import Database
    from omca.policy.gate import PolicyGate
    from omca.policy.operations import PolicyContext

    from .conftest import base_config

    cfg = config_from_dict(base_config(tmp_path))  # allows 127.0.0.1 only
    gate = PolicyGate(Database(cfg.storage.database_path), cfg)

    def verdict(url):
        intent = ActionIntent(action_id="a", task_id="t", operation="browser.navigate", arguments={"url": url},
                              idempotency_key="k")
        return gate.evaluate(intent, PolicyContext(cfg, None))[0].verdict.value

    assert verdict("http://127.0.0.1:8080/") == "ALLOW"
    for url in ("http://127.1:8080/", "http://2130706433/", "http://[::ffff:127.0.0.1]/", "http://x\\@127.0.0.1/",
                "http://u@127.0.0.1/"):
        assert verdict(url) == "DENY", url


async def test_names_that_resolve_to_private_addresses_are_refused(monkeypatch):
    monkeypatch.setattr(netguard, "resolve_private_sync",
                        lambda host: ["10.1.2.3"] if host == "intranet.example.com" else [])
    guard = netguard.UrlGuard(allowed_hosts=["wiki.example.com"])
    with pytest.raises(netguard.BlockedDestination):
        await guard.check("https://intranet.example.com/")
    await guard.check("https://www.example.com/")
    monkeypatch.setattr(netguard, "resolve_private_sync", lambda host: ["10.9.9.9"])
    await guard.check("https://wiki.example.com/")  # the owner allowed this intranet host


async def test_retrieval_checks_every_redirect_hop(site):
    from omca.research.search import HttpRetrievalAdapter, SearchError, fetch_image

    guard = netguard.UrlGuard(allowed_hosts=["127.0.0.1"])
    adapter = HttpRetrievalAdapter("omca-test", guard=guard)
    page = await adapter.retrieve(site.url("/redirect?to=/research/solar.html"), ["markdown"])
    assert page.final_url.endswith("/research/solar.html")
    hidden = f"http://localhost:{site.port}/research/solar.html"
    with pytest.raises(SearchError, match="private network host localhost"):
        await adapter.retrieve(site.url(f"/redirect?to={hidden}"), ["markdown"])
    with pytest.raises(SearchError, match="private network host localhost"):
        await fetch_image(site.url(f"/redirect?to=http://localhost:{site.port}/chart.png"), "omca-test", guard=guard)


@pytest.mark.browser
async def test_browser_task_pages_cannot_reach_unapproved_private_hosts(make_system, site, brain):
    s = await make_system()
    task = s.coord.tasks.create(TaskRequest(goal="probe"), owner=s.coord.owner_key, workflow="browser_task", limits={})
    s.coord.tasks.transition(task.id, TaskStatus.CANCELLED, reason="jobs are dispatched by hand")

    async def run(op, args):
        job = _job(s.coord, task.id, "vm1", op=op, resource="vm1:ui", args=args)
        return await s.coord.workers.dispatch(job.model_copy(update={"attachment_id": "vm1-test"}), 30)

    hidden = f"http://localhost:{site.port}"
    ok = await run("browser.navigate", {"url": site.url("/form.html")})
    assert ok.status == ReceiptStatus.SUCCEEDED
    page = s.worker.browser._sessions["vm1-test"].pages[task.id]
    script = "u => fetch(u).then(r => r.status, () => 'blocked')"
    assert await page.evaluate(script, site.url("/form.html")) == 200
    assert await page.evaluate(script, f"{hidden}/form.html") == "blocked"
    # an HTTP redirect is followed by the browser itself, so the landing page is checked
    redirected = await run("browser.navigate", {"url": site.url(f"/redirect?to={hidden}/form.html")})
    assert redirected.status == ReceiptStatus.FAILED and redirected.error.code.value == "POLICY_DENIED"
    assert page.url == "about:blank"
    image = await run("browser.fetch_image", {"src": site.url(f"/redirect?to={hidden}/chart.png")})
    assert image.status == ReceiptStatus.FAILED and image.error.code.value == "POLICY_DENIED"


# ---------------------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------------------


def test_same_page_links_and_cookie_image_fetches_stay_possible_mutations(tmp_path):
    from omca.config import config_from_dict
    from omca.contracts import ActionCategory as C
    from omca.contracts import ActionIntent, ElementRef, Observation, Rect, Viewport
    from omca.db import Database
    from omca.policy.gate import PolicyGate
    from omca.policy.operations import PolicyContext

    from .conftest import base_config

    cfg = config_from_dict(base_config(tmp_path))
    gate = PolicyGate(Database(cfg.storage.database_path), cfg)
    page = "https://shop.example/cart"

    def link(href):
        return ElementRef(ref="e1", role="link", name="Details", tag="a", href=href, rect=Rect(x=0, y=0, width=9, height=9),
                          fingerprint="fp")

    def category(op, args, element=None):
        o = Observation(observation_id="o", worker_id="vm1", session_id="s", url=page, captured_at=now_iso(),
                        viewport=Viewport(width=800, height=600), elements=[element] if element else [])
        intent = ActionIntent(action_id="a", task_id="t", operation=op, arguments=args, idempotency_key="k",
                              observation_id="o")
        return gate.evaluate(intent, PolicyContext(cfg, o))[0].category

    assert category("browser.click", {"ref": "e1"}, link(page + "#")) == C.UNKNOWN_MUTATION
    assert category("browser.click", {"ref": "e1"}, link("#item-3")) == C.UNKNOWN_MUTATION
    assert category("browser.click", {"ref": "e1"}, link("https://shop.example/help")) == C.NAVIGATION
    bad = {"src": "https://shop.example/account/delete?id=3"}
    assert category("web.fetch_image", bad) == C.READ                 # no cookies: cannot act for the owner
    assert category("browser.fetch_image", bad) == C.UNKNOWN_MUTATION  # session cookies: may act for the owner
    assert category("browser.fetch_image", {"src": "https://shop.example/img/a.png"}) == C.READ
