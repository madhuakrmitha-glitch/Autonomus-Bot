"""Local owner web UI: conversation mirror, delivery without Telegram, commands and buttons."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from omca.api import create_app
from omca.channels import RecordingChannel
from omca.config import config_from_dict
from omca.coordinator import Coordinator

from .conftest import base_config

OWNER = {"Authorization": "Bearer ui-owner-tok"}


def _config(tmp_path, **overrides):
    os.environ["OMCA_T_UI_OWNER"] = "ui-owner-tok"
    return config_from_dict(base_config(tmp_path, ui={"enabled": True},
                                        server={"owner_api_token_ref": "env:OMCA_T_UI_OWNER"}, **overrides))


@pytest.fixture
def web(tmp_path):
    coord = Coordinator(_config(tmp_path))
    return coord, TestClient(create_app(coord))


def test_page_is_public_but_data_needs_the_owner_token(web):
    _, client = web
    page = client.get("/ui")
    assert page.status_code == 200 and "OMCA Assistant" in page.text
    assert client.get("/ui/api/messages").status_code == 401
    assert client.get("/ui/api/messages", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/ui/api/messages", headers=OWNER).json() == {"messages": [], "rev": 0}


def test_ui_is_absent_when_disabled(tmp_path):
    coord = Coordinator(config_from_dict(base_config(tmp_path)))
    assert coord.web_inbox is None
    assert TestClient(create_app(coord)).get("/ui").status_code == 404


def test_command_reply_and_owner_message_appear_in_the_feed(web):
    _, client = web
    r = client.post("/ui/api/messages", data={"text": "/help"}, headers=OWNER)
    assert r.status_code == 200 and "/research" in r.json()["reply"]
    feed = client.get("/ui/api/messages", headers=OWNER).json()
    assert [(m["role"], m["text"][:5]) for m in feed["messages"]] == [("owner", "/help"), ("assistant", "Comma")]
    assert client.get(f"/ui/api/messages?after={feed['rev']}", headers=OWNER).json()["messages"] == []
    assert client.post("/ui/api/messages", data={"text": "  "}, headers=OWNER).status_code == 400


async def test_without_telegram_the_web_ui_is_the_delivery_channel(web):
    coord, client = web
    oid = coord.notifier.text("Task abc123 started", task_id="t1", buttons=[[("Approve once", "omca:apr:apr_X:y")]])
    await coord.notifier.flush()
    assert coord.notifier.result(oid) is not None  # SENT, so delivery steps do not wait for Telegram
    first = client.get("/ui/api/messages", headers=OWNER).json()
    msg = first["messages"][0]
    assert msg["text"] == "Task abc123 started" and msg["buttons"] == [[["Approve once", "omca:apr:apr_X:y"]]]

    coord.notifier.edit(oid, "Task abc123 finished")
    await coord.notifier.flush()
    changed = client.get(f"/ui/api/messages?after={first['rev']}", headers=OWNER).json()["messages"]
    assert [(m["id"], m["text"], m["buttons"]) for m in changed] == [(msg["id"], "Task abc123 finished", None)]

    coord.notifier.document("report.docx", b"PK-docx-bytes", "Your report", task_id="t1")
    await coord.notifier.flush()
    doc = client.get("/ui/api/messages", headers=OWNER).json()["messages"][-1]
    assert doc["kind"] == "document" and doc["filename"] == "report.docx"
    got = client.get(f"/ui/api/files/{doc['id']}", headers=OWNER)
    assert got.content == b"PK-docx-bytes" and 'filename="report.docx"' in got.headers["content-disposition"]
    assert client.get(f"/ui/api/files/{msg['id']}", headers=OWNER).status_code == 404


async def test_telegram_and_web_side_by_side_without_duplicates(tmp_path):
    channel = RecordingChannel()
    coord = Coordinator(_config(tmp_path), channel=channel)
    channel.fail_next = 1  # Telegram is down for the first attempt
    oid = coord.notifier.text("hello")
    await coord.notifier.flush()
    assert coord.notifier.result(oid) is None and len(coord.web_inbox.feed()) == 1
    await coord.notifier.flush()
    assert channel.texts() == ["hello"]
    assert [m["text"] for m in coord.web_inbox.feed()] == ["hello"]

    coord.notifier.edit(oid, "hello again")
    await coord.notifier.flush()
    assert channel.texts() == ["hello again"]
    assert [m["text"] for m in coord.web_inbox.feed()] == ["hello again"]


def test_buttons_go_through_the_owner_interface(web):
    _, client = web
    r = client.post("/ui/api/callback", json={"data": "not-a-button"}, headers=OWNER)
    assert r.json()["reply"] == "Unknown button."
    r = client.post("/ui/api/callback", json={"data": "omca:apr:apr_NOPE:y"}, headers=OWNER)
    assert r.json()["reply"].startswith("Not accepted")


def test_free_text_from_the_ui_starts_an_owner_task(web):
    coord, client = web
    r = client.post("/ui/api/messages", data={"text": "summarise https://example.com/a for me"},
                    files=[("files", ("notes.txt", b"some notes", "text/plain"))], headers=OWNER)
    assert r.status_code == 200
    [task] = coord.tasks.list()
    assert task.goal.startswith("summarise") and task.request.sources == ["https://example.com/a"]
    assert len(task.request.attachments) == 1
    feed = client.get("/ui/api/messages", headers=OWNER).json()["messages"]
    assert feed[0]["role"] == "owner" and "[attached: notes.txt]" in feed[0]["text"]


def test_assets_info_and_task_details(web):
    coord, client = web
    assert client.get("/ui/assets/app.js").headers["content-type"].startswith("text/javascript")
    assert "--accent" in client.get("/ui/assets/app.css").text
    assert client.get("/ui/assets/../api.py").status_code == 404
    assert client.get("/ui/assets/secrets.txt").status_code == 404
    assert client.get("/ui/api/info").status_code == 401
    info = client.get("/ui/api/info", headers=OWNER).json()
    assert info["roles"]["planning"]["route"] == "local" and info["browser_engine"] == "builtin"
    client.post("/ui/api/messages", data={"text": "/research rooftop solar"}, headers=OWNER)
    [task] = coord.tasks.list()
    detail = client.get(f"/ui/api/tasks/{task.id[-6:]}", headers=OWNER).json()
    assert detail["task_id"] == task.id and detail["spend"] == 0 and detail["currency"] == "INR"


def test_owner_messages_are_threaded_under_their_task(web):
    coord, client = web
    r = client.post("/ui/api/messages", data={"text": "/research rooftop solar"}, headers=OWNER).json()
    [task] = coord.tasks.list()
    assert r["task_id"] == task.id
    r2 = client.post("/ui/api/messages", data={"text": f"/status {task.id[-6:]}"}, headers=OWNER).json()
    assert r2["task_id"] == task.id
    owner = [m for m in client.get("/ui/api/messages", headers=OWNER).json()["messages"] if m["role"] == "owner"]
    assert [m["task_id"] for m in owner] == [task.id, task.id]
    r3 = client.post("/ui/api/messages", data={"text": "/budget"}, headers=OWNER).json()
    assert r3["task_id"] is None


async def test_long_poll_returns_as_soon_as_a_message_arrives(web):
    import asyncio

    import httpx

    coord, _ = web
    from omca.api import create_app

    transport = httpx.ASGITransport(app=create_app(coord))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        loop = asyncio.get_running_loop()
        started = loop.time()

        async def later():
            await asyncio.sleep(0.5)
            coord.notifier.text("ping")
            await coord.notifier.flush()

        bg = asyncio.ensure_future(later())
        r = await client.get("/ui/api/messages?after=0&wait=10", headers=OWNER)
        await bg
        assert [m["text"] for m in r.json()["messages"]] == ["ping"]
        assert loop.time() - started < 3
