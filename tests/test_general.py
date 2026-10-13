"""General mode: a chat that searches, reads pages, writes documents and delegates only when that helps."""

from __future__ import annotations

import asyncio
from pathlib import Path

from omca.channels.commands import OwnerInterface
from omca.contracts import TaskRequest, TaskStatus
from omca.coordinator import classify_workflow
from omca.models import register_fixture
from omca.models.providers import fixture_reply
from omca.reports import docx_text
from omca.research.search import StaticSearchAdapter
from omca.workflows.chat import _turns

CHAT = "cnv_aaaaaaaaaaaa"


def calls(*specs: tuple[str, str, dict]) -> dict:
    return fixture_reply("", [{"id": cid, "name": name, "arguments": args} for cid, name, args in specs])


def operations(s, task_id: str) -> list[str]:
    return [r["operation"] for r in s.coord.db.query("SELECT operation FROM actions WHERE task_id=? ORDER BY rowid",
                                                     (task_id,))]


async def test_a_plain_question_gets_a_plain_reply(make_system, brain):
    s = await make_system(worker=False)
    brain.general_script = [fixture_reply("Paris is the capital of France.")]
    t = s.coord.submit_task(TaskRequest(goal="What is the capital of France?", conversation_id=CHAT))
    assert t.workflow == "general_workflow"  # Auto's fallback
    t = await s.wait(t.id)
    assert t.status == TaskStatus.COMPLETED, t.result
    assert t.result["summary"] == "Paris is the capital of France." and t.result["details"]["tools"] == []
    offered = {tool["function"]["name"] for tool in brain.general_bodies[0]["tools"]}
    assert offered == {"web_search", "read_webpage", "write_document", "research_report", "browser_task"}
    assert operations(s, t.id) == ["telegram.send_owner"]
    await s.flush()
    # a chat reply is the whole conversation turn: no acknowledgement, verifying notice or completion echo
    assert s.channel.texts() == ["Paris is the capital of France."]


async def test_searches_and_reads_a_page_before_answering(make_system, site, brain):
    s = await make_system(worker=False)
    page = site.url("/research/solar.html")
    s.coord.search.search_adapter = StaticSearchAdapter("fixture-search", lambda q, n: [
        {"title": "Rooftop solar adoption", "url": page, "snippet": "capacity figures"}])
    brain.general_script = [
        calls(("c1", "web_search", {"query": "rooftop solar capacity India"})),
        calls(("c2", "read_webpage", {"url": page})),
        fixture_reply(f"About 18.4 GW by June 2026 ({page})."),
    ]
    t = await s.wait(s.coord.submit_task(TaskRequest(goal="How much rooftop solar does India have?",
                                                     workflow="general_workflow")).id)
    assert t.status == TaskStatus.COMPLETED, t.result
    assert operations(s, t.id) == ["search.query", "web.retrieve", "telegram.send_owner"]
    assert t.result["details"]["tools"] == ["web_search", "read_webpage"]

    # each result goes back to the model as a tool message answering its call, marked as untrusted content
    second, third = brain.general_bodies[1]["messages"], brain.general_bodies[2]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "c1"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"
    assert "Rooftop solar adoption" in second[-1]["content"] and "<untrusted_source" in second[-1]["content"]
    assert third[-1]["tool_call_id"] == "c2" and "18.4 gigawatts" in third[-1]["content"]
    assert len(third) == len(second) + 2  # the earlier exchange is kept

    # the activity trace records what each tool handed back to the model
    logged = s.coord.tasks.events(t.id, ["tool_result"])
    assert [(e["name"], e["call_id"], e["ok"]) for e in logged] == [("web_search", "c1", True), ("read_webpage", "c2", True)]
    assert logged[0]["result"] == second[-1]["content"]
    assert "18.4 gigawatts" in logged[1]["result"] and len(logged[1]["result"]) <= 3000


async def test_writes_a_word_document_and_sends_it(make_system, brain):
    s = await make_system(worker=False)
    md = "# Support onboarding\n\n## First week\n- Meet the team\n- Read the **runbook**\n\n| Day | Task |\n|---|---|\n| 1 | Setup |\n"
    brain.general_script = [calls(("d1", "write_document", {"title": "Support onboarding", "content": md})),
                            fixture_reply("Here is the onboarding guide.")]
    t = await s.wait(s.coord.submit_task(TaskRequest(goal="Write an onboarding guide", workflow="general_workflow")).id)
    assert t.status == TaskStatus.COMPLETED, t.result
    [doc] = s.coord.artifacts.for_task(t.id, "report")
    data = s.coord.artifacts.read_bytes(doc.artifact_id)
    text = docx_text(data)
    assert {"Support onboarding", "First week", "Meet the team", "Read the runbook", "Setup"} <= set(text.splitlines())
    assert text.count("Support onboarding") == 1  # the repeated title heading is dropped
    assert t.result["artifacts"] == [doc.artifact_id]

    result = brain.general_bodies[1]["messages"][-1]["content"]
    saved = Path(result.split("saved to ")[1].split(";")[0])
    assert saved.exists() and saved.read_bytes() == data
    await s.flush()
    assert s.channel.documents()[-1].data == data


async def test_hands_a_research_report_to_a_child_task_in_the_same_chat(make_system, site, brain):
    s = await make_system(worker=False)
    s.coord.search.search_adapter = StaticSearchAdapter("fixture-search", lambda q, n: [
        {"title": "Rooftop solar adoption", "url": site.url("/research/solar.html")},
        {"title": "Grid integration", "url": site.url("/research/grid.html")},
    ])
    brain.general_script = [calls(("r1", "research_report", {"topic": "Rooftop solar capacity in India"})),
                            fixture_reply("Your cited report is ready.")]
    t = s.coord.submit_task(TaskRequest(goal="Can I get a cited report on rooftop solar in India?", conversation_id=CHAT))
    t = await s.wait(t.id, timeout=120)
    assert t.status == TaskStatus.COMPLETED, t.result

    [child_id] = t.result["details"]["subtasks"]
    child = s.coord.tasks.require(child_id)
    assert child.workflow == "research_report" and child.origin == "subtask" and child.conversation_id == CHAT
    assert child.request.params["parent_task_id"] == t.id and child.status == TaskStatus.COMPLETED
    result = brain.general_bodies[1]["messages"][-1]["content"]
    assert f"#{child_id[-6:]}" in result and "COMPLETED" in result and "Saved to:" in result
    await s.flush()
    assert s.channel.documents(), "the child delivers its own report into the chat"

    # the child is General's own work, not something the owner said in the chat
    follow_up = s.coord.submit_task(TaskRequest(goal="thanks", conversation_id=CHAT))
    assert [m["content"] for m in _turns(s.coord, follow_up)] == [t.goal, "Your cited report is ready."]
    await s.wait(follow_up.id)


class HangingSearch:
    name = "hanging"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def search(self, query: str, limit: int):
        self.started.set()
        await asyncio.Event().wait()


async def test_cancelling_general_cancels_the_child_it_waits_for(make_system, brain):
    s = await make_system(worker=False)
    s.coord.search.search_adapter = adapter = HangingSearch()
    brain.general_script = [calls(("r1", "research_report", {"topic": "anything"}))]
    t = s.coord.submit_task(TaskRequest(goal="research anything", workflow="general_workflow"))
    await asyncio.wait_for(adapter.started.wait(), 30)
    [child] = [x for x in s.coord.tasks.list() if x.origin == "subtask"]

    s.coord.cancel_task(t.id)
    assert (await s.wait(t.id)).status == TaskStatus.CANCELLED
    assert (await s.wait(child.id)).status == TaskStatus.CANCELLED


async def test_general_asks_for_reasoning_and_hands_it_back_with_tool_results(make_system, brain):
    s = await make_system(worker=False)
    s.coord.config.models.route("local").reasoning_body = {"reasoning": {"max_tokens": 1024}}
    s.coord.search.search_adapter = StaticSearchAdapter("fixture-search", lambda q, n: [
        {"title": "Python downloads", "url": "https://www.python.org/downloads/"}])
    details = [{"type": "reasoning.text", "text": "I should look this up.", "signature": "sig-1", "index": 0}]
    first = calls(("c1", "web_search", {"query": "latest Python release"}))
    first["choices"][0]["message"].update(reasoning="I should look this up.", reasoning_details=details)
    brain.general_script = [first, fixture_reply("Python 3.14 is the latest release.")]
    t = await s.wait(s.coord.submit_task(TaskRequest(goal="Latest Python?", workflow="general_workflow")).id)
    assert t.status == TaskStatus.COMPLETED, t.result

    first_body, second_body = brain.general_bodies
    assert first_body["reasoning"] == second_body["reasoning"] == {"max_tokens": 1024}
    assert second_body["messages"][-2]["reasoning_details"] == details  # survives the journal round trip
    thoughts = [e.get("thought") for e in s.coord.tasks.events(t.id, ["model_done"])]
    assert thoughts == ["I should look this up.", None]

    # other workflows never ask for reasoning
    seen: list[dict] = []
    register_fixture("brain", lambda route, body: seen.append(body) or fixture_reply("hi"))
    await s.wait(s.coord.submit_task(TaskRequest(goal="hi", workflow="chat_workflow")).id)
    assert seen and "reasoning" not in seen[0]


async def test_without_a_tool_capable_route_it_still_answers(make_system, brain):
    s = await make_system(worker=False)
    s.coord.config.models.route("local").supports_tools = False
    t = await s.wait(s.coord.submit_task(TaskRequest(goal="hello", workflow="general_workflow")).id)
    assert t.status == TaskStatus.COMPLETED and t.result["summary"] == "Happy to help."
    assert "tools" not in brain.general_bodies[0]
    assert any("no planning model route supports tool calls" in x for x in t.result["limitations"])


async def test_general_command_and_auto_routing(make_system):
    assert classify_workflow("what's the capital of France?") == "general_workflow"
    assert classify_workflow("go to https://example.com and log in") == "browser_task"
    s = await make_system(worker=False)
    iface = OwnerInterface(s.coord)
    assert await iface.handle_message(s.owner(), "/general") == "Usage: /general <message>"
    await iface.handle_message(s.owner(), "/general summarise our chat")
    [task] = s.coord.tasks.list()
    assert task.workflow == "general_workflow" and task.goal == "summarise our chat"


async def test_chat_workflow_records_its_reply_as_the_result(make_system):
    s = await make_system(worker=False)
    register_fixture("brain", lambda route, body: fixture_reply("Hello there!"))
    t = await s.wait(s.coord.submit_task(TaskRequest(goal="hi", workflow="chat_workflow", conversation_id=CHAT)).id)
    assert t.status == TaskStatus.COMPLETED and t.result["summary"] == "Hello there!"
    nxt = s.coord.submit_task(TaskRequest(goal="and again", workflow="chat_workflow", conversation_id=CHAT))
    assert _turns(s.coord, nxt) == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello there!"}]
    await s.wait(nxt.id)
