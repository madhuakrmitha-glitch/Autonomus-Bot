"""Unit tests for security-critical pieces: approval binding, classification, artifacts, reports, budget."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from omca.artifacts import ArtifactError, ArtifactStore, safe_filename, sniff_media_type
from omca.budget import BudgetBlocked, BudgetLedger, CallEstimate
from omca.config import config_from_dict
from omca.contracts import ActionCategory as C
from omca.contracts import ActionIntent, ElementRef, Observation, Rect, Viewport
from omca.db import Database
from omca.evidence import EvidenceStore, locate_excerpt, numbers_in
from omca.policy import ApprovalError, ApprovalService, Identity, PolicyContext, PolicyGate
from omca.reports import ReportInput, ReportSection, build_docx, validate_docx
from omca.util import iso, now_iso, utcnow

from .conftest import OWNER_ID, base_config


@pytest.fixture
def cfg(tmp_path):
    return config_from_dict(base_config(tmp_path, None, policy={"contacts": [{"name": "A", "kind": "email",
                                                                              "address": "a@example.com"}]}))


@pytest.fixture
def db(cfg):
    return Database(cfg.storage.database_path)


def el(ref="e1", role="button", name="Go", **kw) -> ElementRef:
    return ElementRef(ref=ref, role=role, name=name, tag=kw.pop("tag", "button"), rect=Rect(x=0, y=0, width=10, height=10),
                      fingerprint=kw.pop("fingerprint", f"fp-{ref}-{name}"), **kw)


def obs(*elements: ElementRef, focused: str | None = None, url="https://shop.example/cart") -> Observation:
    return Observation(observation_id="obs_1", worker_id="vm1", session_id="s", url=url, captured_at=now_iso(),
                       viewport=Viewport(width=1280, height=800), elements=list(elements), focused_ref=focused)


def decide(gate, cfg, op, args, observation=None, hit=None):
    intent = ActionIntent(action_id="act_1", task_id="tsk_1", operation=op, arguments=args, idempotency_key="k",
                          observation_id=observation.observation_id if observation else None)
    d, _ = gate.evaluate(intent, PolicyContext(cfg, observation, hit_test=hit))
    return d


@pytest.mark.parametrize("element,expected", [
    (el(role="link", name="Pricing", tag="a", href="https://shop.example/pricing"), C.NAVIGATION),
    (el(role="link", name="Delete my account", tag="a", href="https://shop.example/x"), C.CONSEQUENTIAL),
    (el(role="link", name="Next", tag="a", href="https://shop.example/account/delete"), C.CONSEQUENTIAL),
    (el(role="button", name="Place order", in_form=True, input_type="submit"), C.CONSEQUENTIAL),
    (el(role="button", name="Next", in_form=True, input_type="submit"), C.CONSEQUENTIAL),  # submit regardless of label
    (el(role="button", name="Show more", expanded=False), C.NAVIGATION),
    (el(role="button", name="Show more"), C.UNKNOWN_MUTATION),
    (el(role="tab", name="Reviews", tag="div"), C.NAVIGATION),
    (el(role="checkbox", name="Gift wrap", tag="input", input_type="checkbox"), C.DRAFT),
    (el(role="canvas", name="", tag="canvas"), C.UNKNOWN_MUTATION),
    (el(role="switch", name="Notifications", tag="div"), C.UNKNOWN_MUTATION),
])
def test_click_classification(cfg, db, element, expected):
    gate = PolicyGate(db, cfg)
    d = decide(gate, cfg, "browser.click", {"ref": element.ref}, obs(element))
    assert d.category == expected


@pytest.mark.parametrize("key,focus,expected", [
    ("PageDown", None, C.NAVIGATION),
    ("Enter", el("e1", "textbox", "Search", tag="input", input_type="text", editable=True, in_form=True), C.CONSEQUENTIAL),
    ("Enter", el("e1", "link", "Docs", tag="a", href="https://shop.example/docs"), C.NAVIGATION),
    ("Control+Enter", None, C.CONSEQUENTIAL),
    ("e", None, C.UNKNOWN_MUTATION),                              # app shortcut (e.g. archive)
    ("e", el("e1", "textbox", "Message", tag="input", input_type="text", editable=True), C.DRAFT),
    ("a", el("e1", "textbox", "Password", tag="input", input_type="password", editable=True), C.FORBIDDEN),
    ("Delete", None, C.UNKNOWN_MUTATION),
    ("Control+c", None, C.READ),
])
def test_keyboard_classification(cfg, db, key, focus, expected):
    gate = PolicyGate(db, cfg)
    o = obs(*( [focus] if focus else []), focused=focus.ref if focus else None)
    d = decide(gate, cfg, "browser.press", {"key": key}, o)
    assert d.category == expected


def test_policy_denials_and_staleness(cfg, db):
    gate = PolicyGate(db, cfg)
    assert decide(gate, cfg, "browser.navigate", {"url": "javascript:alert(1)"}).verdict.value == "DENY"
    assert decide(gate, cfg, "browser.navigate", {"url": "file:///etc/passwd"}).verdict.value == "DENY"
    assert decide(gate, cfg, "browser.navigate", {"url": "http://10.0.0.5/admin"}).verdict.value == "DENY"
    assert decide(gate, cfg, "shell.exec", {"cmd": "rm -rf /"}).verdict.value == "DENY"
    assert decide(gate, cfg, "credential.export", {"target": "browser"}).verdict.value == "DENY"
    assert decide(gate, cfg, "email.send", {"draft_artifact_id": "x", "to": ["evil@x.com"]}).category == C.FORBIDDEN
    assert decide(gate, cfg, "email.send", {"draft_artifact_id": "x", "to": ["a@example.com"]}).verdict.value == "REQUIRE_APPROVAL"
    pw = el("e1", "textbox", "Password", tag="input", input_type="password", editable=True)
    assert decide(gate, cfg, "browser.type", {"ref": "e1", "text": "x"}, obs(pw)).category == C.FORBIDDEN
    # malformed arguments are rejected before classification
    d = decide(gate, cfg, "browser.scroll", {"dy": 10**9})
    assert d.verdict.value == "DENY" and d.error_code.value == "MALFORMED_TOOL_CALL"
    # an element action needs a fresh observation
    old = obs(el()).model_copy(update={"captured_at": iso(utcnow() - timedelta(minutes=5))})
    assert decide(gate, cfg, "browser.click", {"ref": "e1"}, old).error_code.value == "STALE_OBSERVATION"
    assert decide(gate, cfg, "browser.click", {"ref": "e9"}, obs(el())).category == C.FORBIDDEN


def test_standing_rules_are_narrow(tmp_path):
    rule = {"id": "cookies", "description": "cookie banner", "operation": "browser.click",
            "origin": "https://docs.example.com", "element_name_pattern": "Accept cookies", "max_uses": 1}
    cfg = config_from_dict(base_config(tmp_path, None, policy={"standing_rules": [rule]}))
    gate = PolicyGate(Database(cfg.storage.database_path), cfg)
    accept = el(name="Accept cookies")
    assert decide(gate, cfg, "browser.click", {"ref": "e1"}, obs(accept, url="https://docs.example.com/a")).verdict.value == "ALLOW"
    assert decide(gate, cfg, "browser.click", {"ref": "e1"}, obs(accept, url="https://evil.example/a")).verdict.value == "REQUIRE_APPROVAL"
    assert decide(gate, cfg, "browser.click", {"ref": "e1"},
                  obs(el(name="Delete all"), url="https://docs.example.com/a")).verdict.value == "REQUIRE_APPROVAL"
    gate.record_rule_use("cookies", "act_1")
    assert decide(gate, cfg, "browser.click", {"ref": "e1"}, obs(accept, url="https://docs.example.com/a")).verdict.value == "REQUIRE_APPROVAL"
    with pytest.raises(ValueError):  # a rule without any scope is refused
        config_from_dict({"policy": {"standing_rules": [{"id": "x", "description": "all", "operation": "*"}]}})


def test_approval_binding_rejects_every_kind_of_change(cfg, db):
    gate = PolicyGate(db, cfg)
    svc = ApprovalService(db, cfg)
    owner = f"telegram:{OWNER_ID}"
    intent = ActionIntent(action_id="act_1", task_id="tsk_1", operation="email.send",
                          arguments={"draft_artifact_id": "art_1", "to": ["a@example.com"]}, idempotency_key="k")

    def fresh():
        ap = svc.create(intent, owner=owner, policy_version=gate.version, destination="a@example.com", consequence="send",
                        preview="send", preconditions={"draft_sha256": "abc"}, fingerprint=None)
        svc.decide(ap.approval_id, Identity("telegram", str(OWNER_ID)), "approve")
        return ap.approval_id

    cases = [
        dict(intent=intent.model_copy(update={"arguments": {**intent.arguments, "to": ["b@example.com"]}})),
        dict(destination="b@example.com"),
        dict(current_preconditions={"draft_sha256": "changed"}),
        dict(current_policy_version="pol_other"),
        dict(owner="telegram:42"),
        dict(intent=intent.model_copy(update={"action_id": "act_2"})),
    ]
    for change in cases:
        aid = fresh()
        kw = dict(owner=owner, fingerprint=None, destination="a@example.com", current_preconditions={"draft_sha256": "abc"},
                  current_policy_version=gate.version)
        it = change.pop("intent", intent)
        kw.update(change)
        with pytest.raises(ApprovalError):
            svc.consume(aid, it, **kw)
    aid = fresh()
    ok = dict(owner=owner, fingerprint=None, destination="a@example.com", current_preconditions={"draft_sha256": "abc"},
              current_policy_version=gate.version)
    svc.consume(aid, intent, **ok)
    with pytest.raises(ApprovalError):  # the nonce is single-use
        svc.consume(aid, intent, **ok)
    # tampering with the stored binding breaks the signature
    aid = fresh()
    db.execute("UPDATE approvals SET destination='attacker@x.com' WHERE id=?", (aid,))
    with pytest.raises(ApprovalError):
        svc.consume(aid, intent, **{**ok, "destination": "attacker@x.com"})


def test_artifacts_are_confined_typed_and_checksummed(cfg, db):
    store = ArtifactStore(db, cfg)
    png = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (40, 20), "red").save(png, "PNG")
    a = store.put_image("tsk_1", "evidence", "../../evil/shot.png", png.getvalue())
    assert a.manifest.rel_path == "tasks/tsk_1/evidence/shot.png" and a.manifest.width == 40
    with pytest.raises(ArtifactError):
        store.put("tsk_1", "report", "report.docx", b"MZ\x90\x00 not a docx")  # content type checked, not the name
    with pytest.raises(ArtifactError):
        store.abs_path("../outside.txt")
    with pytest.raises(ArtifactError):
        store.task_dir("../etc", "input")
    b = store.put("tsk_1", "evidence", "shot.png", png.getvalue(), media_type="image/png")
    assert b.manifest.name == "shot-2.png"  # never overwrites silently
    crop = store.crop(a.manifest.artifact_id, Rect(x=5, y=5, width=10, height=10), upscale_to=100)
    assert crop.manifest.parent_artifact_id == a.manifest.artifact_id and crop.manifest.crop.width == 10
    store.abs_path(a.manifest.rel_path).write_bytes(b"tampered")
    with pytest.raises(ArtifactError):
        store.read_bytes(a.manifest.artifact_id)
    assert safe_filename("CON.txt") == "_CON.txt" and safe_filename("a/b\\c:d.PNG") == "c_d.png"
    assert sniff_media_type(b"%PDF-1.7") == "application/pdf"


def test_report_validation_detects_missing_claims_and_bad_citations(cfg, db):
    ev = EvidenceStore(db)
    e1 = ev.record("tsk_1", kind="text_excerpt", source_url="https://a.example", captured_at=now_iso(),
                   content_for_hash="x", excerpt="Capacity reached 18.4 gigawatts.")
    c1 = ev.add_claim("tsk_1", "Capacity reached 18.4 GW.", [e1.evidence_id])
    c2 = ev.add_claim("tsk_1", "An uncited statement.", [])
    assert c2.support == "unsupported"
    inp = ReportInput(title="T", task_id="tsk_1", goal="g", summary_claims=[c1], sections=[ReportSection("S", [c2])],
                      evidence=[e1], limitations=[], disagreements=[], method={}, timezone="Asia/Kolkata",
                      generated_at=now_iso())
    data, labels = build_docx(inp)
    ok = validate_docx(data, expected_sha256=None, claims=[c1, c2], labels=labels, evidence_ids={e1.evidence_id})
    assert ok["ok"], ok
    missing = c1.model_copy(update={"text": "A claim that is not in the document."})
    bad = validate_docx(data, expected_sha256="0" * 64, claims=[missing], labels=labels, evidence_ids=set())
    assert not bad["ok"] and any("checksum" in p for p in bad["problems"]) and any("missing" in p for p in bad["problems"])
    assert any("not a recorded evidence item" in p for p in bad["problems"])


def test_excerpt_location_and_numbers():
    src = "Rooftop solar capacity in India reached 18.4 gigawatts by the end of June 2026, according to the bulletin."
    assert locate_excerpt(src, "capacity in India reached 18.4 gigawatts")
    assert locate_excerpt(src, "capacity in India reached 19.4 gigawatts") is None
    assert numbers_in("grew 1,200 units (12%) to 3.50") == {"1200", "12", "3.5"}


def test_time_billed_endpoint_sessions_are_reserved_and_settled(tmp_path):
    route = {"id": "ded", "provider": "hf_dedicated_endpoint", "base_url": "https://e.example/v1", "model": "tgi",
             "license": "apache-2.0", "open_weights": True, "enabled": True, "billing": "time",
             "pricing": {"currency": "USD", "per_hour": 2.0, "source": "price page", "effective_date": "2026-10-01"},
             "dedicated": {"controller": "manual", "max_session_hours": 1, "initialization_minutes": 6}}
    cfg = config_from_dict(base_config(tmp_path, None, budget={"monthly_ceiling": 300, "reserve_margin": 0.0,
                                                               "fx": {"USD": {"rate": 100, "source": "rbi", "effective_date": "2026-10-01"}}},
                                       models={"allow_fixture_routes": True, "require_probes": False, "roles": {},
                                               "routes": [route]}))
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    r = cfg.models.routes[0]
    with pytest.raises(BudgetBlocked):  # no approved running session yet
        ledger.reserve(r, CallEstimate(10), task_id=None, purpose="x", caller="t")
    sid = ledger.reserve_endpoint_session(r, hours=1)
    assert ledger.status().reserved == pytest.approx(220.0)  # (1 h + 6 min init) * 2 USD * 100
    with pytest.raises(BudgetBlocked):  # a second window would exceed the shared ceiling
        ledger.reserve_endpoint_session(r, hours=1)
    ledger.reserve(r, CallEstimate(10), task_id=None, purpose="x", caller="t")  # covered by the session
    cost = ledger.settle_endpoint_session(sid, r, stopped_at=utcnow() + timedelta(minutes=30))
    assert cost == pytest.approx(120.0, rel=0.02)  # 36 min (30 + 6 init) * 2 USD/h * 100 INR/USD


def test_unpriced_hosted_routes_stay_disabled(tmp_path):
    route = {"id": "h", "provider": "openai_compatible", "base_url": "https://api.example/v1", "model": "Qwen/Qwen3.5-35B-A3B",
             "license": "apache-2.0", "open_weights": True, "enabled": True, "billing": "tokens",
             "pricing": {"currency": "USD", "input_per_million": 0.1, "output_per_million": 0.4}}
    cfg = config_from_dict(base_config(tmp_path, None, models={"roles": {}, "routes": [route]}))
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    assert "source and effective date" in ledger.route_billing_problem(cfg.models.routes[0])
    with pytest.raises(BudgetBlocked):
        ledger.reserve(cfg.models.routes[0], CallEstimate(10), task_id=None, purpose="x", caller="t")


async def test_endpoint_window_pauses_and_settles(tmp_path):
    import httpx

    from omca.models.endpoints import start_window, stop_window

    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    import os

    os.environ["OMCA_T_HF"] = "hf-token"
    route = {"id": "ded", "provider": "hf_dedicated_endpoint", "base_url": "https://e.example/v1", "model": "tgi",
             "license": "apache-2.0", "open_weights": True, "enabled": True, "billing": "time",
             "pricing": {"currency": "USD", "per_hour": 1.0, "source": "price page", "effective_date": "2026-10-01"},
             "dedicated": {"controller": "huggingface", "namespace": "me", "name": "qwen", "api_token_ref": "env:OMCA_T_HF"}}
    cfg = config_from_dict(base_config(tmp_path, None, budget={"fx": {"USD": {"rate": 90, "source": "rbi",
                                                                              "effective_date": "2026-10-01"}}},
                                       models={"allow_fixture_routes": True, "require_probes": False, "roles": {},
                                               "routes": [route]}))
    from omca.models.endpoints import HuggingFaceEndpointController

    ctl = HuggingFaceEndpointController(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    r = cfg.models.routes[0]
    sid = await start_window(ledger, r, 0.5, ctl)
    await stop_window(ledger, r, sid, ctl)
    assert calls == ["/v2/endpoint/me/qwen/resume", "/v2/endpoint/me/qwen/pause"]
    assert ledger.db.scalar("SELECT status FROM endpoint_sessions WHERE id=?", (sid,)) == "SETTLED"


def _login_cfg(tmp_path, auto=False):
    return config_from_dict(base_config(tmp_path, None, policy={"auto_approve_browser": auto}, browser={
        "credentials": [{"id": "gh", "origin": "https://github.com", "username_ref": "env:GH_USER",
                         "password_ref": "env:GH_PASS"}]}))


def test_credential_placeholders_are_origin_bound(tmp_path):
    cfg = _login_cfg(tmp_path)
    gate = PolicyGate(Database(cfg.storage.database_path), cfg)
    pw = el("e1", "textbox", "Password", tag="input", input_type="password", editable=True)
    user = el("e2", "textbox", "Username", tag="input", input_type="text", editable=True)
    on_site = "https://github.com/login"
    ph = "{{cred:gh.password}}"
    assert decide(gate, cfg, "browser.type", {"ref": "e1", "text": ph}, obs(pw, url=on_site)).category == C.DRAFT
    assert decide(gate, cfg, "browser.type", {"ref": "e2", "text": "{{cred:gh.username}}"},
                  obs(user, url=on_site)).category == C.DRAFT
    # wrong origin, unknown id, password into a plain field and literal text into a password field all stay forbidden
    assert decide(gate, cfg, "browser.type", {"ref": "e1", "text": ph},
                  obs(pw, url="https://evil.example/login")).category == C.FORBIDDEN
    assert decide(gate, cfg, "browser.type", {"ref": "e1", "text": "{{cred:nope.password}}"},
                  obs(pw, url=on_site)).category == C.FORBIDDEN
    assert decide(gate, cfg, "browser.type", {"ref": "e2", "text": ph}, obs(user, url=on_site)).category == C.FORBIDDEN
    assert decide(gate, cfg, "browser.type", {"ref": "e1", "text": "hunter2"}, obs(pw, url=on_site)).category == C.FORBIDDEN


def test_auto_approve_browser_only_covers_browser_actions(tmp_path):
    cfg = _login_cfg(tmp_path, auto=True)
    gate = PolicyGate(Database(cfg.storage.database_path), cfg)
    submit = el(role="button", name="Place order", in_form=True, input_type="submit")
    d = decide(gate, cfg, "browser.click", {"ref": "e1"}, obs(submit))
    assert d.category == C.CONSEQUENTIAL and d.verdict.value == "ALLOW"
    # forbidden stays forbidden, and non-browser consequential actions still need the owner
    assert decide(gate, cfg, "browser.navigate", {"url": "javascript:alert(1)"}).verdict.value == "DENY"
    assert decide(gate, cfg, "email.send", {"draft_artifact_id": "x", "to": ["a@example.com"]}).verdict.value in {
        "DENY", "REQUIRE_APPROVAL"}
    assert decide(gate, cfg, "file.delete", {"rel_path": "a.txt"}).verdict.value == "REQUIRE_APPROVAL"


def test_placeholder_substitution(tmp_path, monkeypatch):
    from omca.credentials import substitute

    cfg = _login_cfg(tmp_path)
    monkeypatch.setenv("GH_PASS", "s3cret!")
    assert substitute("{{cred:gh.password}}", cfg.browser.credentials, "https://github.com/login") == "s3cret!"
    assert substitute("plain text", cfg.browser.credentials, "https://evil.example") == "plain text"
    with pytest.raises(PermissionError):
        substitute("{{cred:gh.password}}", cfg.browser.credentials, "https://evil.example/")
    monkeypatch.delenv("GH_USER", raising=False)
    with pytest.raises(PermissionError):
        substitute("{{cred:gh.username}}", cfg.browser.credentials, "https://github.com/")
