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


# ---------------------------------------------------------------------------------------------------------
# Email: only the approved recipients, only this task's draft, and the owner sees what is sent
# ---------------------------------------------------------------------------------------------------------


def _draft(**headers) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    for k, v in {"To": "asha@example.com", "From": "owner@example.com", "Subject": "Weekly numbers", **headers}.items():
        msg[k] = v
    msg.set_content("Hi Asha, the numbers are attached.")
    return bytes(msg)


class _Transport:
    def __init__(self, data: bytes):
        self.data = data

    async def download_input(self, job_id, artifact_id):
        return self.data


@pytest.mark.parametrize("extra", [{"Cc": "eve@example.com"}, {"Bcc": "eve@example.com"},
                                   {"Resent-To": "eve@example.com"}])
async def test_email_send_refuses_unapproved_header_recipients(smtp_sink, tmp_path, extra):
    from omca.config import SmtpConfig
    from omca.contracts import WorkerJob
    from omca.worker.agent import JobContext, JobFailure
    from omca.worker.office import make_office_handlers

    send = make_office_handlers(SmtpConfig(host="127.0.0.1", port=smtp_sink.port, starttls=False,
                                           from_address="owner@example.com"))["email.send"]

    def job(data):
        j = WorkerJob(job_id="job_1", task_id="tsk_1", action_id="act_1", worker_id="vm1", lease_id="l", resource="r",
                      fencing_token=1, operation="email.send", arguments={"draft_artifact_id": "art_d", "to": ["asha@example.com"]},
                      idempotency_key="k", deadline=now_iso(), lease_expires_at=now_iso(), issued_at=now_iso(),
                      declared_inputs=["art_d"])
        return j, JobContext(j, _Transport(data), tmp_path, {})

    with pytest.raises(JobFailure) as e:
        await send(*job(_draft(**extra)))
    assert e.value.code.value == "POLICY_DENIED" and smtp_sink.messages == []
    await send(*job(_draft()))
    assert [m["to"] for m in smtp_sink.messages] == [["asha@example.com"]]  # the envelope is the approved list


def test_email_inputs_must_belong_to_the_task_and_the_preview_shows_the_message(tmp_path):
    from omca.config import config_from_dict
    from omca.contracts import ActionIntent
    from omca.policy.operations import PolicyContext

    from .conftest import base_config

    c = Coordinator(config_from_dict(base_config(tmp_path, policy={"contacts": [
        {"name": "Asha", "kind": "email", "address": "asha@example.com"}]})))
    mine = c.artifacts.put("tsk_mine", "output", "draft.eml", _draft()).manifest.artifact_id
    theirs = c.artifacts.put("tsk_other", "output", "draft.eml", _draft()).manifest.artifact_id
    attached = c.artifacts.put(None, "input", "draft.eml", _draft()).manifest.artifact_id

    def verdict(aid, inputs=()):
        intent = ActionIntent(action_id="a", task_id="tsk_mine", operation="email.send", idempotency_key="k",
                              arguments={"draft_artifact_id": aid, "to": ["asha@example.com"]})
        ctx = PolicyContext(c.config, None, task_id="tsk_mine", artifacts=c.artifacts.get, task_inputs=list(inputs))
        return c.policy.evaluate(intent, ctx)[0]

    assert verdict(mine).verdict.value == "REQUIRE_APPROVAL"
    denied = verdict(theirs)
    assert denied.verdict.value == "DENY" and "does not belong" in denied.reason
    assert verdict("art_missing").verdict.value == "DENY"
    assert verdict(attached, inputs=[attached]).verdict.value == "REQUIRE_APPROVAL"  # the owner attached it

    preview = c._email_preview({"draft_artifact_id": mine, "to": ["asha@example.com"]})
    assert "Subject: Weekly numbers" in preview and "the numbers are attached" in preview
    cc = c.artifacts.put("tsk_mine", "output", "cc.eml", _draft(Cc="eve@example.com")).manifest.artifact_id
    assert "WARNING" in c._email_preview({"draft_artifact_id": cc, "to": ["asha@example.com"]})


# ---------------------------------------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------------------------------------


def test_token_estimate_covers_non_latin_text_and_message_overhead():
    from omca.budget import MESSAGE_OVERHEAD_TOKENS, estimate_message_tokens, estimate_text_tokens

    hindi = "भारत में छत पर सौर ऊर्जा " * 50          # about 1 token per 1-2 characters in practice
    chinese = "屋顶太阳能在印度的普及情况" * 50
    assert estimate_text_tokens(hindi) >= len(hindi)
    assert estimate_text_tokens(chinese) >= 1.5 * len(chinese)
    english = "Rooftop solar adoption in India " * 50
    assert len(english) / 4 < estimate_text_tokens(english) <= len(english) / 3 + 5
    msgs = [{"role": "user", "content": "hi"}] * 10
    assert estimate_message_tokens(msgs) == 10 * (estimate_text_tokens("hi") + MESSAGE_OVERHEAD_TOKENS)


def test_a_call_costing_more_than_its_reservation_is_flagged(tmp_path):
    from omca.budget import BudgetLedger, CallEstimate, Usage
    from omca.config import config_from_dict
    from omca.db import Database

    from .conftest import base_config

    route = {"id": "h", "provider": "openai_compatible", "base_url": "https://api.example/v1",
             "model": "Qwen/Qwen3.5-35B-A3B", "license": "apache-2.0", "open_weights": True, "enabled": True,
             "billing": "tokens", "pricing": {"currency": "USD", "input_per_million": 1.0, "output_per_million": 1.0,
                                              "source": "price page", "effective_date": "2026-10-01"}}
    cfg = config_from_dict(base_config(tmp_path, None, budget={"fx": {"USD": {"rate": 90, "source": "rbi",
                                                                              "effective_date": "2026-10-01"}}},
                                       models={"roles": {}, "routes": [route]}))
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    seen = []
    ledger.on_overrun = lambda *a: seen.append(a)
    r = cfg.models.routes[0]
    cid = ledger.reserve(r, CallEstimate(100, max_output_tokens=100), task_id=None, purpose="x", caller="t")
    cost = ledger.settle(cid, r, Usage(input_tokens=100_000, output_tokens=50_000, source="provider"), images=0,
                         latency_ms=1)
    assert seen and seen[0][0] == cid and seen[0][3] == cost
    assert "exceeded the reservation" in ledger.db.scalar("SELECT error FROM model_calls WHERE id=?", (cid,))


# ---------------------------------------------------------------------------------------------------------
# Dedicated endpoints: a session that might still be running is never settled
# ---------------------------------------------------------------------------------------------------------


def _endpoint_cfg(tmp_path):
    import os

    from omca.config import config_from_dict

    from .conftest import base_config

    os.environ["OMCA_T_HF"] = "hf-token"
    route = {"id": "ded", "provider": "hf_dedicated_endpoint", "base_url": "https://e.example/v1", "model": "tgi",
             "license": "apache-2.0", "open_weights": True, "enabled": True, "billing": "time",
             "pricing": {"currency": "USD", "per_hour": 1.0, "source": "price page", "effective_date": "2026-10-01"},
             "dedicated": {"controller": "huggingface", "namespace": "me", "name": "qwen", "api_token_ref": "env:OMCA_T_HF"}}
    return config_from_dict(base_config(tmp_path, None, budget={"fx": {"USD": {"rate": 90, "source": "rbi",
                                                                               "effective_date": "2026-10-01"}}},
                                        models={"allow_fixture_routes": True, "require_probes": False, "roles": {},
                                                "routes": [route]}))


class _Ctl:
    def __init__(self, resume_ok=True, pause_ok=True):
        self.resume_ok, self.pause_ok, self.calls = resume_ok, pause_ok, []

    async def resume(self, route):
        self.calls.append("resume")
        if not self.resume_ok:
            raise RuntimeError("resume failed")

    async def pause(self, route):
        self.calls.append("pause")
        if not self.pause_ok:
            raise RuntimeError("pause failed")


async def test_failed_resume_or_pause_keeps_the_session_counting(tmp_path):
    from omca.budget import BudgetLedger
    from omca.db import Database
    from omca.models.endpoints import EndpointControlError, start_window, stop_window

    cfg = _endpoint_cfg(tmp_path)
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    r = cfg.models.routes[0]

    def status(sid):
        return ledger.db.scalar("SELECT status FROM endpoint_sessions WHERE id=?", (sid,))

    ctl = _Ctl(resume_ok=False)  # resume failed but the pause works: nothing keeps running
    with pytest.raises(RuntimeError):
        await start_window(ledger, r, 0.5, ctl)
    assert ctl.calls == ["resume", "pause"]
    assert ledger.db.scalar("SELECT COUNT(*) FROM endpoint_sessions WHERE status='SETTLED'") == 1

    with pytest.raises(EndpointControlError):  # neither works: the endpoint may be running
        await start_window(ledger, r, 0.5, _Ctl(resume_ok=False, pause_ok=False))
    sid = ledger.db.scalar("SELECT id FROM endpoint_sessions WHERE status='RUNNING'")
    assert sid and ledger.overdue_endpoint_sessions() == [(sid, "ded")]  # maintenance retries the pause

    sid2 = await start_window(ledger, r, 0.5, _Ctl())
    with pytest.raises(RuntimeError):
        await stop_window(ledger, r, sid2, _Ctl(pause_ok=False))
    assert status(sid2) == "RUNNING" and (sid2, "ded") in ledger.overdue_endpoint_sessions()


async def test_maintenance_retries_the_pause_and_settles_only_after_it_works(tmp_path, monkeypatch):
    from omca.channels import RecordingChannel
    from omca.models import endpoints

    cfg = _endpoint_cfg(tmp_path)
    c = Coordinator(cfg, channel=RecordingChannel())
    r = cfg.models.routes[0]
    sid = await endpoints.start_window(c.ledger, r, 0.5, _Ctl())
    c.ledger.end_endpoint_window(sid)
    c.db.execute("UPDATE endpoint_sessions SET started_at=?, reservation=0.01 WHERE id=?",
                 ("2026-10-12T00:00:00.000Z", sid))
    failing = _Ctl(pause_ok=False)
    monkeypatch.setattr(endpoints, "controller_for", lambda route: failing)
    await c.enforce_endpoint_windows()
    row = c.db.one("SELECT status, cost, reservation FROM endpoint_sessions WHERE id=?", (sid,))
    assert row["status"] == "RUNNING" and row["cost"] > row["reservation"]
    with c.db.tx() as tx:  # the accrued running cost, not the stale reservation, counts against the budget
        assert c.ledger._period_spend(tx, c.ledger.period())[1] == pytest.approx(row["cost"])
    await c.enforce_endpoint_windows()
    assert failing.calls == ["pause"]  # at most one attempt per minute
    c._pause_attempts.clear()
    monkeypatch.setattr(endpoints, "controller_for", lambda route: _Ctl())
    await c.enforce_endpoint_windows()
    assert c.db.scalar("SELECT status FROM endpoint_sessions WHERE id=?", (sid,)) == "SETTLED"


# ---------------------------------------------------------------------------------------------------------
# Standing rules
# ---------------------------------------------------------------------------------------------------------


def test_standing_rule_paths_cannot_escape_and_names_alone_do_not_scope(tmp_path):
    from omca.config import config_from_dict
    from omca.contracts import ActionIntent
    from omca.policy.operations import PolicyContext

    from .conftest import base_config

    rule = {"id": "drafts", "description": "overwrite drafts", "operation": "file.write", "path_prefix": "drafts/"}
    c = Coordinator(config_from_dict(base_config(tmp_path, policy={"standing_rules": [rule]})))
    art = c.artifacts.put("tsk_1", "output", "r.txt", b"report").manifest.artifact_id

    def verdict(path):
        intent = ActionIntent(action_id="a", task_id="tsk_1", operation="file.write", idempotency_key="k",
                              arguments={"artifact_id": art, "rel_path": path, "overwrite": True})
        return c.policy.evaluate(intent, PolicyContext(c.config, None, task_id="tsk_1", artifacts=c.artifacts.get))[0]

    assert verdict("drafts/weekly.docx").verdict.value == "ALLOW"
    for path in ("drafts/../final/weekly.docx", "drafts/..\\final.docx", "draftsX/weekly.docx", "/drafts/x.docx"):
        assert verdict(path).verdict.value == "REQUIRE_APPROVAL", path
    with pytest.raises(ValueError):
        config_from_dict({"policy": {"standing_rules": [{"id": "x", "description": "d", "operation": "browser.click",
                                                         "element_name_pattern": "Accept cookies"}]}})
