"""Browser Use engine: policy classification, config paths and the worker handler (with a fake runner)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omca.config import BrowserAgentConfig, BrowserAttachment, config_from_dict
from omca.contracts import ActionCategory, ErrorCode
from omca.policy.operations import OPERATIONS, BrowserAgentTaskArgs
from omca.worker import browser_agent
from omca.worker.agent import JobFailure
from omca.worker.browser_use_runner import RESULT_PREFIX

from .conftest import base_config

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _classify(tmp_path, require_approval: bool):
    cfg = config_from_dict(base_config(tmp_path, browser={"engine": "browser_use",
                                                          "agent": {"require_approval": require_approval}}))
    spec = OPERATIONS["browser.agent_task"]
    args = BrowserAgentTaskArgs(goal="find the opening hours", start_url="https://example.com")
    return spec.classify(args, {}, SimpleNamespace(config=cfg))


def test_agent_runs_ask_once_unless_the_owner_disabled_approval(tmp_path):
    asked = _classify(tmp_path / "a", True)
    assert asked.category == ActionCategory.CONSEQUENTIAL and "find the opening hours" in asked.consequence
    free = _classify(tmp_path / "b", False)
    assert free.category == ActionCategory.NAVIGATION and "disabled approval" in free.reason


def test_relative_interpreter_path_keeps_the_virtualenv_symlink(tmp_path):
    cfg = config_from_dict(base_config(tmp_path, browser={"agent": {"python": "../venv/bin/python"}}), base=tmp_path / "config")
    assert cfg.browser.agent.python == str(tmp_path / "venv" / "bin" / "python")


class FakeManager:
    def __init__(self, att):
        self.attachments = {att.id: att}
        self.released: list[str] = []

    async def release_session(self, attachment_id):
        self.released.append(attachment_id)


class FakeCtx:
    def __init__(self, workspace):
        self.workspace = workspace
        self.uploads: list[tuple[str, str, bytes]] = []

    async def upload(self, kind, name, data, *, media_type=None, meta=None):
        self.uploads.append((kind, name, data))
        return "art_final"


def _runner(tmp_path: Path, payload: dict) -> Path:
    script = tmp_path / "fake_runner.py"
    script.write_text(
        "import json, sys\n"
        "req = json.loads(sys.stdin.read())\n"
        f"json.dump(req, open({str(tmp_path / 'request.json')!r}, 'w'))\n"
        "print('noise from the agent')\n"
        f"print({RESULT_PREFIX!r} + json.dumps({payload!r}))\n")
    return script


def _setup(tmp_path, monkeypatch, payload, **att_overrides):
    monkeypatch.setattr(browser_agent, "RUNNER", _runner(tmp_path, payload))
    att = BrowserAttachment(**{"id": "vm1-chrome", "worker_id": "vm1", "mode": "persistent_profile", "channel": "chrome",
                               "user_data_dir": str(tmp_path / "profile"), "authorized": True, **att_overrides})
    settings = browser_agent.BrowserAgentSettings(BrowserAgentConfig(python=sys.executable, max_steps=7),
                                                  "http://127.0.0.1:8765/v1", "gw-secret")
    manager = FakeManager(att)
    handler = browser_agent.make_browser_agent_handlers(manager, settings)["browser.agent_task"]
    job = SimpleNamespace(job_id="job_1", task_id="tsk_1", attachment_id=att.id,
                          arguments={"goal": "find the opening hours", "start_url": "https://example.com"})
    return handler, job, manager, FakeCtx(tmp_path / "ws")


async def test_handler_runs_the_agent_and_returns_its_final_page(tmp_path, monkeypatch):
    payload = {"ok": True, "done": True, "success": True, "final_result": "Open 9-5", "steps": 3,
               "duration_seconds": 12.5, "urls": ["https://example.com"], "errors": [],
               "final": {"url": "https://example.com/hours", "title": "Hours", "text": "Open 9-5",
                         "screenshot_b64": base64.b64encode(PNG).decode()}}
    handler, job, manager, ctx = _setup(tmp_path, monkeypatch, payload)
    res = await handler(job, ctx)
    assert manager.released == ["vm1-chrome"]  # the profile is freed before the agent opens it
    assert res.result["final_result"] == "Open 9-5" and res.result["url"] == "https://example.com/hours"
    assert res.result["screenshot_artifact_id"] == "art_final" and res.artifact_ids == ["art_final"]
    assert ctx.uploads[0][0] == "screenshot" and ctx.uploads[0][2] == PNG
    sent = json.loads((tmp_path / "request.json").read_text())
    assert sent["api_key"] == "gw-secret" and sent["base_url"] == "http://127.0.0.1:8765/v1"
    assert sent["model"] == "vision" and sent["max_steps"] == 7 and sent["user_data_dir"] == str(tmp_path / "profile")


async def test_agent_failures_are_reported_as_uncertain(tmp_path, monkeypatch):
    handler, job, _, ctx = _setup(tmp_path, monkeypatch, {"ok": False, "error": "TIMEOUT", "message": "too slow"})
    with pytest.raises(JobFailure) as e:
        await handler(job, ctx)
    assert e.value.code == ErrorCode.TIMEOUT and e.value.uncertain


async def test_unauthorized_or_unsupported_attachments_are_refused(tmp_path, monkeypatch):
    handler, job, manager, ctx = _setup(tmp_path, monkeypatch, {"ok": True}, authorized=False)
    with pytest.raises(JobFailure) as e:
        await handler(job, ctx)
    assert e.value.code == ErrorCode.AUTH_REQUIRED and manager.released == []
    handler, job, _, ctx = _setup(tmp_path, monkeypatch, {"ok": True}, mode="cdp", cdp_url="http://127.0.0.1:9222")
    with pytest.raises(JobFailure) as e:
        await handler(job, ctx)
    assert e.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
