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


def test_each_chat_keeps_its_own_messages_and_tasks(web):
    coord, client = web
    a, b = "cnv_aaaaaaaaaaaa", "cnv_bbbbbbbbbbbb"
    ra = client.post("/ui/api/messages", data={"text": "hello from chat a", "conversation_id": a}, headers=OWNER).json()
    rb = client.post("/ui/api/messages", data={"text": "/budget", "conversation_id": b}, headers=OWNER).json()
    assert ra["conversation_id"] == a and rb["task_id"] is None
    [task] = coord.tasks.list()
    assert task.conversation_id == a and task.request.conversation_id == a
    assert client.get(f"/ui/api/tasks/{task.id[-6:]}", headers=OWNER).json()["conversation_id"] == a
    feed = client.get("/ui/api/messages", headers=OWNER).json()["messages"]
    by_chat = {c: [m["text"] for m in feed if m["conversation_id"] == c and m["role"] == "owner"] for c in (a, b)}
    assert "hello from chat a" in by_chat[a] and "/budget" in by_chat[b] and "hello from chat a" not in by_chat[b]
    assert all(m["conversation_id"] == a for m in feed if m["task_id"] == task.id)  # the ack inherits the task's chat
    assert client.post("/ui/api/messages", data={"text": "hi", "conversation_id": "../x"}, headers=OWNER).status_code == 400


def test_chat_workflow_only_sees_its_own_conversation(web):
    from omca.workflows.chat import _turns

    coord, client = web
    for chat, text in (("cnv_aaaaaaaaaaaa", "first in a"), ("cnv_bbbbbbbbbbbb", "only in b"), ("cnv_aaaaaaaaaaaa", "second in a")):
        client.post("/ui/api/messages", data={"text": text, "conversation_id": chat}, headers=OWNER)
    newest = coord.tasks.list(limit=1)[0]
    assert newest.goal == "second in a"
    turns = _turns(coord, newest)
    assert [t["content"] for t in turns] == ["first in a"]  # queued tasks have no answer yet; chat b is never mixed in


async def test_activity_feed_shows_what_the_assistant_is_doing(make_system):
    import httpx

    from omca.contracts import TaskRequest

    os.environ["OMCA_T_UI_OWNER"] = "ui-owner-tok"
    s = await make_system(worker=False, ui={"enabled": True}, server={"owner_api_token_ref": "env:OMCA_T_UI_OWNER"})
    transport = httpx.ASGITransport(app=create_app(s.coord))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/ui/api/activity")).status_code == 401
        cursor = (await client.get("/ui/api/activity?after=-1", headers=OWNER)).json()
        assert cursor["events"] == []

        t = s.coord.submit_task(TaskRequest(goal="say hello", workflow="chat_workflow", conversation_id="cnv_aaaaaaaaaaaa"))
        await s.wait(t.id)

        live = (await client.get(f"/ui/api/activity?after={cursor['last_id']}", headers=OWNER)).json()
        kinds = [e["kind"] for e in live["events"]]
        assert {"progress", "model_start", "model_done", "action_intent", "action_receipt", "status"} <= set(kinds)
        assert kinds.index("model_start") < kinds.index("model_done")
        done = next(e for e in live["events"] if e["kind"] == "model_done")
        assert done["task_id"] == t.id and done["data"]["role"] == "planning" and done["data"]["text"]
        assert next(e for e in live["events"] if e["kind"] == "action_intent")["data"]["arguments"]["text"]
        assert live["last_id"] == live["events"][-1]["id"]

        # a chat or a task can be loaded whole; a later cursor sees nothing new
        by_chat = (await client.get("/ui/api/activity?conversation=cnv_aaaaaaaaaaaa", headers=OWNER)).json()
        by_task = (await client.get(f"/ui/api/activity?task_id={t.id[-6:]}", headers=OWNER)).json()
        assert [e["id"] for e in by_chat["events"]] == [e["id"] for e in by_task["events"]] == [e["id"] for e in live["events"]]
        assert (await client.get("/ui/api/activity?conversation=cnv_bbbbbbbbbbbb", headers=OWNER)).json()["events"] == []
        assert (await client.get(f"/ui/api/activity?after={live['last_id']}", headers=OWNER)).json()["events"] == []
        assert (await client.get("/ui/api/activity?task_id=nope", headers=OWNER)).status_code == 404


async def test_model_reasoning_is_part_of_the_activity(make_system):
    from omca.contracts import TaskRequest
    from omca.models import register_fixture
    from omca.models.providers import fixture_reply

    s = await make_system(worker=False, ui={"enabled": True})
    register_fixture("brain", lambda route, body: fixture_reply("<think>The user greets me, so I greet back.</think>Hello there!"))
    t = s.coord.submit_task(TaskRequest(goal="hi", workflow="chat_workflow"))
    await s.wait(t.id)
    [done] = [e for e in s.coord.web_inbox.activity(task_id=t.id) if e["kind"] == "model_done"]
    assert done["data"]["thought"] == "The user greets me, so I greet back."
    assert done["data"]["text"] == "Hello there!"  # the reasoning is shown separately from the reply
