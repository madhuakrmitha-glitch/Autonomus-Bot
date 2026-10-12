"""HTTP API, OpenClaw bridge, model gateway and contract files (spec sections 14 and 19)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omca.api import create_app
from omca.config import config_from_dict, load_config
from omca.coordinator import Coordinator
from omca.models import fixture_reply, register_fixture
from omca.schemas import contract_document, openapi_document

from .conftest import OWNER_ID, STRANGER_ID, base_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def api(tmp_path, site):
    os.environ.update({"OMCA_T_OWNER": "owner-tok", "OMCA_T_BRIDGE": "bridge-tok", "OMCA_T_GW": "gw-tok"})
    register_fixture("gw", lambda route, body: fixture_reply("hello from an open model", prompt_tokens=10, completion_tokens=4))
    cfg = config_from_dict(base_config(
        tmp_path, site,
        server={"owner_api_token_ref": "env:OMCA_T_OWNER", "model_gateway_token_ref": "env:OMCA_T_GW"},
        telegram={"mode": "openclaw", "openclaw": {"bridge_token_ref": "env:OMCA_T_BRIDGE"}},
        models={"allow_fixture_routes": True, "require_probes": False,
                "roles": {"planning": "open", "vision": "open"},
                "routes": [{"id": "open", "provider": "fixture", "fixture": "gw", "model": "Qwen/Qwen3.5-9B",
                            "license": "apache-2.0", "open_weights": True, "enabled": True, "billing": "none",
                            "modalities": ["text", "image"]}]}))
    coord = Coordinator(cfg)
    return coord, TestClient(create_app(coord))


OWNER = {"Authorization": "Bearer owner-tok"}
BRIDGE = {"Authorization": "Bearer bridge-tok"}
GW = {"Authorization": "Bearer gw-tok"}


def test_owner_routes_require_the_owner_token(api):
    coord, c = api
    for path in ("/tasks", "/budget", "/schedules"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/tasks", json={"goal": "Report on rooftop solar", "workflow": "research_report"}, headers=OWNER)
    assert r.status_code == 200 and r.json()["status"] == "QUEUED"
    tid = r.json()["task_id"]
    assert c.get(f"/tasks/{tid[-6:]}", headers=OWNER).json()["task_id"] == tid
    assert c.post(f"/tasks/{tid}/cancel", headers=OWNER).json()["status"] == "CANCELLED"
    assert c.get("/budget", headers=OWNER).json()["ceiling"] == 2000.0
    # an owner identity can never be supplied in the body
    r = c.post("/tasks", json={"goal": "x", "owner": "attacker"}, headers=OWNER)
    assert r.status_code == 422


def test_openclaw_bridge_uses_the_verified_sender(api):
    coord, c = api
    assert c.post("/bridge/openclaw/message", json={"sender_id": str(OWNER_ID), "text": "/tasks"}).status_code == 401
    r = c.post("/bridge/openclaw/message", json={"sender_id": str(STRANGER_ID), "text": "/research steal"}, headers=BRIDGE)
    assert r.status_code == 200 and r.json()["reply"] is None and coord.tasks.list() == []
    r = c.post("/bridge/openclaw/message", json={"sender_id": str(OWNER_ID), "text": "/research rooftop solar",
                                                 "message_id": "m1"}, headers=BRIDGE)
    assert r.status_code == 200 and len(coord.tasks.list()) == 1
    # duplicate delivery of the same message is ignored
    c.post("/bridge/openclaw/message", json={"sender_id": str(OWNER_ID), "text": "/research rooftop solar",
                                             "message_id": "m1"}, headers=BRIDGE)
    assert len(coord.tasks.list()) == 1
    # OpenClaw reserves /status and /approve, so the catch-all /omca prefix works too
    r = c.post("/bridge/openclaw/message", json={"sender_id": str(OWNER_ID), "text": "/omca tasks"}, headers=BRIDGE)
    assert "queued" in r.json()["reply"]
    r = c.post("/bridge/openclaw/callback", json={"sender_id": str(STRANGER_ID), "data": "omca:apr:apr_X:y"}, headers=BRIDGE)
    assert r.json()["reply"] == "Unknown button." or r.json()["reply"].startswith("Not accepted")


def test_model_gateway_is_allowlisted_and_metered(api):
    coord, c = api
    body = {"model": "planning", "messages": [{"role": "user", "content": "hi"}]}
    assert c.post("/v1/chat/completions", json=body).status_code == 401
    r = c.post("/v1/chat/completions", json=body, headers=GW)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello from an open model"
    for model in ("gpt-4o", "omca/claude-3"):
        r = c.post("/v1/chat/completions", json={**body, "model": model}, headers=GW)
        assert r.status_code == 403
    models = c.get("/v1/models", headers=GW).json()["data"]
    assert {m["id"] for m in models} >= {"planning", "vision"}
    # streaming is metered as well
    with c.stream("POST", "/v1/chat/completions", json={**body, "stream": True}, headers=GW) as s:
        chunks = [ln for ln in s.iter_lines() if ln.startswith("data:")]
    assert chunks[-1] == "data: [DONE]" and "hello from an open model" in "".join(chunks)
    rows = coord.db.query("SELECT caller, status, route_id FROM model_calls ORDER BY created_at")
    assert [r["caller"] for r in rows] == ["openclaw", "openclaw"] and {r["route_id"] for r in rows} == {"open"}
    assert all(r["status"] == "SETTLED" for r in rows)


def test_contract_files_match_the_code():
    schemas = json.loads((ROOT / "contracts" / "schemas.json").read_text())
    assert schemas == json.loads(json.dumps(contract_document())), "run `omca contracts export`"
    import yaml

    assert yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text()) == json.loads(json.dumps(openapi_document()))
    paths = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())["paths"]
    for route in ("/tasks", "/tasks/{task_id}", "/tasks/{task_id}/cancel", "/approvals/{approval_id}/decisions",
                  "/workers/register", "/workers/{worker_id}/jobs", "/workers/{worker_id}/receipts", "/budget"):
        assert route in paths, route


def test_example_configuration_is_valid_and_activates_nothing(tmp_path):
    cfg = load_config(ROOT / "config" / "application.example.yaml")
    coord = Coordinator(cfg.model_copy(update={"storage": cfg.storage.model_copy(
        update={"data_dir": tmp_path / "d", "workspace_root": tmp_path / "w"})}))
    for r in cfg.models.routes:
        assert not coord.registry.state(r).usable, r.id
    st = coord.ledger.status()
    assert st.hosted_routes_enabled == []
    from omca.integrations.openclaw import translate

    text = translate(cfg)
    doc = json.loads(text.split("\n", 4)[-1])
    assert doc["models"]["mode"] == "replace" and doc["agents"]["defaults"]["model"]["fallbacks"] == []
    assert doc["tools"]["allow"] == ["omca__*"] and doc["channels"]["telegram"]["dmPolicy"] == "allowlist"
    assert "${" in doc["channels"]["telegram"]["botToken"]  # never a literal secret


def test_literal_secrets_are_rejected():
    with pytest.raises(ValueError):
        config_from_dict({"telegram": {"bot_token_ref": "123456:ABCdefLiteralToken"}})
    with pytest.raises(ValueError):
        config_from_dict({"workers": [{"id": "vm1", "credential_ref": "hunter2"}]})


def test_mcp_server_exposes_only_task_tools():
    import asyncio

    from omca.mcp_server import build_server

    tools = asyncio.run(build_server("http://127.0.0.1:1", "t").list_tools())
    names = {t.name for t in tools}
    assert names == {"create_task", "task_status", "list_tasks", "cancel_task", "pause_task", "resume_task",
                     "steer_task", "budget", "task_image"}
    for t in tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", {})
        assert "owner" not in json.dumps(schema).lower()
