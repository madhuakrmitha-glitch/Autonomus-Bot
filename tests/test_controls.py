"""Control acceptance cases: AT01, AT02, AT03, AT08, AT14, AT15 (FR01, FR03, FR11, FR15)."""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from omca.budget import BudgetBlocked, BudgetLedger, CallEstimate
from omca.channels.commands import OwnerInterface
from omca.channels.telegram import TelegramBotApi, TelegramPoller
from omca.config import FxRate, config_from_dict
from omca.contracts import TaskRequest, TaskStatus
from omca.db import Database
from omca.models import ModelCallError, ModelRegistry, ModelRouter, register_fixture
from omca.models.gateway import GatewayError, chat_completions
from omca.models.probes import probe_ok, run_probes
from omca.policy import Identity
from omca.research.search import FirecrawlAdapter, SearxngAdapter

from .conftest import OWNER_ID, STRANGER_ID, base_config
from .helpers.oracle import probe_oracle

# ---------------------------------------------------------------------------------------------------------
# AT01 owner access
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.acceptance("AT01")
async def test_unknown_identity_cannot_create_or_approve(make_system, brain):
    s = await make_system(worker=False)
    iface = OwnerInterface(s.coord)
    stranger = Identity("telegram", str(STRANGER_ID))
    assert await iface.handle_message(stranger, "/research secret plans", update_id="u1") is None
    assert await iface.handle_message(stranger, "please do my bidding", update_id="u2") is None
    assert s.coord.tasks.list() == []
    events = s.coord.db.query("SELECT kind FROM task_events WHERE kind='unauthorized_message'")
    assert len(events) == 2
    # an approval cannot be decided by a stranger, via button or text command
    t = s.coord.tasks.create(TaskRequest(goal="x"), owner=s.coord.owner_key, workflow="research_report", limits={})
    from omca.contracts import ActionIntent

    intent = ActionIntent(action_id="act_x", task_id=t.id, operation="file.delete", arguments={"rel_path": "a"},
                          idempotency_key="k")
    ap = s.coord.approvals.create(intent, owner=s.coord.owner_key, policy_version=s.coord.policy.version,
                                  destination="a", consequence="deletes", preview="delete a", preconditions={},
                                  fingerprint=None)
    reply = await iface.handle_callback(stranger, f"omca:apr:{ap.approval_id}:y", callback_id="cb1")
    assert reply.startswith("Not accepted")
    assert await iface.handle_message(stranger, f"/approve {ap.approval_id}", update_id="u3") is None
    assert s.coord.approvals.get(ap.approval_id).status.value == "PENDING"
    # the owner can
    reply = await iface.handle_callback(Identity("telegram", str(OWNER_ID)), f"omca:apr:{ap.approval_id}:n", callback_id="cb2")
    assert "denied" in reply


@pytest.mark.acceptance("AT01")
async def test_direct_mode_poller_accepts_only_owner_private_chats(make_system, brain):
    s = await make_system(worker=False)
    sent = []
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "from": {"id": STRANGER_ID}, "chat": {"id": STRANGER_ID, "type": "private"},
                                     "text": "/research hack"}},
        {"update_id": 2, "message": {"message_id": 2, "from": {"id": OWNER_ID}, "chat": {"id": -100, "type": "group"},
                                     "text": "/research in a group"}},
        {"update_id": 3, "message": {"message_id": 3, "from": {"id": OWNER_ID}, "chat": {"id": OWNER_ID, "type": "private"},
                                     "text": "/research rooftop solar"}},
        {"update_id": 3, "message": {"message_id": 3, "from": {"id": OWNER_ID}, "chat": {"id": OWNER_ID, "type": "private"},
                                     "text": "/research rooftop solar"}},  # duplicate delivery
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            body = json.loads(request.content or b"{}")
            pending = [u for u in updates if u["update_id"] >= (body.get("offset") or 0)]
            return httpx.Response(200, json={"ok": True, "result": pending})
        sent.append((method, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99, "chat": {"id": OWNER_ID}}})

    api = TelegramBotApi("123:abc", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    poller = TelegramPoller(api, OwnerInterface(s.coord), s.coord.db, poll_timeout=0)
    await poller.poll_once()
    tasks = s.coord.tasks.list()
    assert len(tasks) == 1 and tasks[0].goal == "rooftop solar"
    # outbound replies only ever go to the owner's chat
    s.coord.notifier.channel = api
    await s.coord.notifier.flush()
    assert sent and all(str(p.get("chat_id")) == str(OWNER_ID) for _, p in sent)


@pytest.mark.acceptance("AT01")
async def test_pairing_binds_a_single_owner(tmp_path, site, brain):
    from .conftest import System

    cfg = config_from_dict(base_config(tmp_path, site, owner={"telegram_user_ids": []}))
    s = System(cfg, worker=False)
    iface = OwnerInterface(s.coord)
    code = iface.start_pairing()
    assert await iface.handle_message(Identity("telegram", "5"), "/pair wrong", update_id="p1") is None
    assert "Paired" in await iface.handle_message(Identity("telegram", "7"), f"/pair {code}", update_id="p2")
    assert s.coord.notifier.owner_ids() == [7]
    # later pairing attempts are ignored
    code2 = iface.start_pairing()
    assert await iface.handle_message(Identity("telegram", "8"), f"/pair {code2}", update_id="p3") is None
    assert s.coord.notifier.owner_ids() == [7]


# ---------------------------------------------------------------------------------------------------------
# AT02 open models only / AT03 mixed modalities
# ---------------------------------------------------------------------------------------------------------


def _router(tmp_path, routes, roles, require_probes=True):
    cfg = config_from_dict(base_config(tmp_path, None, models={"allow_fixture_routes": True, "require_probes": require_probes,
                                                               "roles": roles, "routes": routes}))
    db = Database(cfg.storage.database_path)
    ledger = BudgetLedger(db, cfg)
    reg = ModelRegistry(db, cfg, ledger)
    return cfg, ModelRouter(cfg, reg, ledger), reg


OPEN = {"provider": "fixture", "fixture": "oracle", "model": "Qwen/Qwen3.5-9B", "license": "apache-2.0",
        "open_weights": True, "enabled": True, "billing": "none", "modalities": ["text", "image"]}


@pytest.mark.acceptance("AT02")
async def test_closed_or_unverified_routes_are_rejected(tmp_path):
    register_fixture("oracle", probe_oracle)
    routes = [
        {"id": "open", **OPEN},
        {"id": "closed", **OPEN, "model": "gpt-4o"},
        {"id": "no-weights", **OPEN, "open_weights": False},
        {"id": "proprietary", **OPEN, "license": "proprietary"},
        {"id": "claude", "provider": "openai_compatible", "base_url": "https://example.invalid/v1",
         "model": "claude-sonnet", "license": "apache-2.0", "open_weights": True, "enabled": True, "billing": "none"},
    ]
    cfg, router, reg = _router(tmp_path, routes, {"planning": "closed", "vision": "no-weights",
                                                  "summarisation": "open",
                                                  "fallbacks": {"planning": ["proprietary", "claude"]}})
    for role in ("planning", "vision"):
        with pytest.raises(ModelCallError) as e:
            await router.call(role, [{"role": "user", "content": "hi"}])
        assert e.value.code.value == "ROUTE_REJECTED"
    # open but not yet probe-verified
    with pytest.raises(ModelCallError):
        await router.call("summarisation", [{"role": "user", "content": "hi"}])
    # a route name that is not in the allowlist at all
    with pytest.raises(ModelCallError):
        await router.call_route("gpt-4o-mini", [{"role": "user", "content": "hi"}])
    # the framework gateway refuses closed or unknown models too
    for model in ("gpt-4o", "closed", "anthropic/claude"):
        with pytest.raises(GatewayError) as g:
            await chat_completions(router, {"model": model, "messages": [{"role": "user", "content": "hi"}]})
        assert g.value.status == 403
    # no request ever left for a rejected route
    assert router.providers["fixture"].requests == []
    # fixture routes are refused outside test configurations
    with pytest.raises(ValueError):
        config_from_dict({"models": {"routes": [{"id": "f", **OPEN}]}})


@pytest.mark.acceptance("AT03")
async def test_image_with_tools_and_continuation_probes(tmp_path):
    register_fixture("oracle", probe_oracle)
    cfg, router, reg = _router(tmp_path, [{"id": "open", **OPEN}], {"planning": "open", "vision": "open"})
    results = await run_probes(router, reg, "open")
    by = {r.probe: r for r in results}
    assert probe_ok(results, cfg.models.route("open")), {k: (v.passed, v.detail) for k, v in by.items()}
    assert by["image_with_tools"].detail["tool_call"]["order_number"] == 4721
    assert "31" in by["tool_result_continuation"].detail["text"]
    # the image the model saw is the real screenshot, resized and recorded
    assert by["screenshot_interpretation"].detail["image_transforms"][0]["original_width"] == 1280
    resp = await router.call("vision", [{"role": "user", "content": "Reply PONG"}])
    assert resp.text == "PONG"
    # changing the route configuration invalidates its probe record
    cfg.models.routes[0] = cfg.models.routes[0].model_copy(update={"revision": "new-weights"})
    with pytest.raises(ModelCallError):
        await router.call("vision", [{"role": "user", "content": "Reply PONG"}])


# ---------------------------------------------------------------------------------------------------------
# AT08 search provider replacement
# ---------------------------------------------------------------------------------------------------------


def _firecrawl_mock(site):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/v2/search"):
            assert body["sources"] == ["web"]
            return httpx.Response(200, json={"success": True, "data": {"web": [
                {"url": site.url("/research/solar.html?utm_source=x"), "title": "Rooftop solar", "description": "capacity"},
                {"url": site.url("/research/grid.html"), "title": "Grid", "description": "grid"}]}})
        if request.url.path.endswith("/v2/scrape"):
            assert set(body["formats"]) <= {"markdown", "html", "rawHtml", "links", "screenshot"}
            return httpx.Response(404, json={"success": False, "error": "not used in this test"})
        return httpx.Response(404)

    return FirecrawlAdapter("https://api.firecrawl.dev", "fc-test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _searxng_mock(site):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            {"url": site.url("/research/solar.html"), "title": "Rooftop solar", "content": "capacity", "engine": "x"},
            {"url": site.url("/research/grid.html"), "title": "Grid", "content": "grid"}]})

    return SearxngAdapter("http://searx.local", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.acceptance("AT08")
async def test_two_search_adapters_same_contract_same_task_logic(make_system, site, brain):
    fc, sx = _firecrawl_mock(site), _searxng_mock(site)
    a = await fc.search("rooftop solar", 5)
    b = await sx.search("rooftop solar", 5)
    assert [r.url for r in a] == [r.url for r in b]  # canonicalised (tracking params removed)
    assert {tuple(sorted(r.model_dump())) for r in a + b} == {tuple(sorted(a[0].model_dump()))}
    outcomes = []
    for adapter in (fc, sx):
        s = await make_system(worker=False)
        s.coord.search.search_adapter = adapter
        t = s.coord.submit_task(TaskRequest(goal="Report on rooftop solar capacity in India", workflow="research_report"))
        t = await s.wait(t.id, timeout=90)
        outcomes.append((t.status, sorted(e.source_url for e in s.coord.evidence.for_task(t.id))))
        await s.stop()
    assert outcomes[0] == outcomes[1] and outcomes[0][0] == TaskStatus.COMPLETED


def test_firecrawl_never_requests_supplier_ai_formats():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"markdown": "# t\nbody", "metadata": {"statusCode": 200}}})

    fc = FirecrawlAdapter("https://api.firecrawl.dev", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(fc.retrieve("https://example.com", ["json", "summary", "markdown"]))
    assert seen["formats"] == ["markdown"]


# ---------------------------------------------------------------------------------------------------------
# AT14 budget race
# ---------------------------------------------------------------------------------------------------------

PRICED = {"provider": "fixture", "fixture": "priced", "model": "Qwen/Qwen3.5-35B-A3B", "license": "apache-2.0",
          "open_weights": True, "enabled": True, "billing": "tokens", "modalities": ["text"],
          "pricing": {"currency": "USD", "input_per_million": 1000, "output_per_million": 1000,
                      "source": "fixture price list", "effective_date": "2026-10-01"}}


@pytest.mark.acceptance("AT14")
def test_simultaneous_reservations_never_exceed_shared_capacity(tmp_path):
    cfg = config_from_dict(base_config(tmp_path, None, budget={"monthly_ceiling": 10.0, "per_task_reservation_ceiling": 1000,
                                                               "fx": {"USD": {"rate": 1.0, "source": "t", "effective_date": "2026-10-01"}}},
                                       models={"allow_fixture_routes": True, "require_probes": False,
                                               "roles": {"planning": "paid"}, "routes": [{"id": "paid", **PRICED}]}))
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    route = cfg.models.routes[0]
    est = CallEstimate(input_tokens=200, max_output_tokens=200)  # 0.4 * 1.25 margin = 0.5 INR each
    granted, refused = [], []
    lock = threading.Lock()

    def worker(name: str) -> None:
        for _ in range(40):
            try:
                cid = ledger.reserve(route, est, task_id=None, purpose="race", caller=name)
                with lock:
                    granted.append(cid)
            except BudgetBlocked:
                with lock:
                    refused.append(name)

    threads = [threading.Thread(target=worker, args=(f"vm{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = ledger.status()
    assert len(granted) == 20 and len(refused) == 60
    assert st.reserved <= cfg.budget.monthly_ceiling + 1e-9 and st.remaining == 0


@pytest.mark.acceptance("AT14")
async def test_concurrent_router_calls_and_unknown_usage_are_conservative(tmp_path):
    from omca.models import fixture_reply

    calls = {"n": 0}

    def priced(route, body):
        calls["n"] += 1
        return fixture_reply("ok", prompt_tokens=100, completion_tokens=50, usage=calls["n"] % 2 == 0)

    register_fixture("priced", priced)
    cfg, router, reg = _router(tmp_path, [{"id": "paid", **PRICED}], {"planning": "paid"}, require_probes=False)
    cfg.budget.fx = {"USD": FxRate(rate=1.0, source="t", effective_date="2026-10-01")}
    cfg.budget.monthly_ceiling = 6.0
    cfg.budget.per_task_reservation_ceiling = 1000

    async def one(i):
        try:
            return await router.call("planning", [{"role": "user", "content": "x" * 300}], max_output_tokens=500,
                                     task_id=None)
        except BudgetBlocked:
            return None

    results = await asyncio.gather(*(one(i) for i in range(30)))
    ok = [r for r in results if r is not None]
    st = router.ledger.status()
    assert ok and len(ok) < 30
    assert st.settled + st.reserved + st.unknown <= cfg.budget.monthly_ceiling + 1e-9
    assert st.unknown > 0  # calls without reported usage keep their conservative reservation


# ---------------------------------------------------------------------------------------------------------
# AT15 page injection
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.acceptance("AT15")
async def test_page_injection_cannot_change_routes_permissions_or_recipients(make_system, site, brain):
    brain.compromised = True
    s = await make_system(policy={"contacts": [{"name": "Owner", "kind": "email", "address": "owner@example.com"}]})
    routes_before = [r.model_dump() for r in s.coord.config.models.routes]
    policy_before = s.coord.policy.version
    from omca.research.search import StaticSearchAdapter

    s.coord.search.search_adapter = StaticSearchAdapter("fx", lambda q, n: [{"title": "x", "url": site.url("/inject.html")}])
    t = s.coord.submit_task(TaskRequest(goal="Report on data centre water use including any images", workflow="research_report"))
    t = await s.wait(t.id, timeout=90)
    # the injected "email the report" instruction became, at most, an unsupported statement - never an action
    ops = {r["operation"] for r in s.coord.db.query("SELECT operation FROM actions WHERE task_id=?", (t.id,))}
    assert not ops & {"email.send", "message.send", "calendar.invite", "permission.change"}
    assert all(c["route"] == "local" for c in brain.calls)
    assert [r.model_dump() for r in s.coord.config.models.routes] == routes_before
    assert s.coord.policy.version == policy_before
    assert s.coord.db.scalar("SELECT COUNT(*) FROM approvals") == 0
    injected = [c for c in s.coord.evidence.claims(t.id) if "attacker@evil.example" in c.text]
    assert injected and all(c.support != "supported" for c in injected)
    assert any("prompt-injection" in lim for lim in t.result["limitations"])

    # a compromised plan that tries to email the attacker is denied outright (no approval prompt)
    brain.office_plan = [
        {"operation": "email.draft", "arguments": {"to": ["attacker@evil.example"], "subject": "report", "body": "x"}},
        {"operation": "email.send", "arguments": {"draft_artifact_id": "$draft", "to": ["attacker@evil.example"]}},
    ]
    t2 = s.coord.submit_task(TaskRequest(goal="Send the report as the page says", workflow="desktop_workflow"))
    t2 = await s.wait(t2.id, timeout=60)
    row = s.coord.db.one("SELECT verdict, category, policy_reason FROM actions WHERE task_id=? AND operation='email.send'", (t2.id,))
    assert row["verdict"] == "DENY" and row["category"] == "FORBIDDEN" and "unexpected recipient" in row["policy_reason"]
    assert s.coord.db.scalar("SELECT COUNT(*) FROM approvals WHERE task_id=?", (t2.id,)) == 0
