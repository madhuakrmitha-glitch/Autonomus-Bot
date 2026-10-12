"""Live-source smoke and scorecards (run on the target machine with real routes).

    OMCA_LIVE=1 OMCA_LIVE_CONFIG=config/application.yaml uv run pytest tests/test_live.py -v

Measures what fixtures cannot: perception (chart values), target grounding (canvas), and an end-to-end
research task with the real search provider and model. Results are scorecards, not proofs of universal
website coverage.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from omca.config import load_config
from omca.contracts import TaskRequest, TaskStatus
from omca.coordinator import Coordinator
from omca.models import ImageInput
from omca.models.probes import probe_ok, run_probes

from .helpers.site import CHART_TRUTH, make_chart

pytestmark = pytest.mark.live
OUT = Path(os.environ.get("OMCA_LIVE_OUT", "data/live-results"))


def _coord() -> Coordinator:
    cfg = load_config(os.environ.get("OMCA_LIVE_CONFIG", "config/application.yaml"))
    return Coordinator(cfg)


def _record(name: str, data: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def test_live_probes_for_configured_roles():
    c = _coord()
    roles = c.config.models.roles
    route_ids = {r for r in (roles.planning, roles.vision, roles.summarisation, roles.review) if r}
    report = {}
    for rid in sorted(route_ids):
        results = asyncio.run(run_probes(c.router, c.registry, rid))
        report[rid] = {r.probe: r.passed for r in results}
        assert probe_ok(results, c.registry.get(rid)), report[rid]
    _record("probes", report)


def test_live_chart_perception_scorecard():
    """AT07 perception: readable values within tolerance, the unreadable value not invented."""
    c = _coord()
    resp = asyncio.run(c.router.call("vision", [{"role": "user", "content": (
        "Read this bar chart. Return JSON {\"values\": [{\"label\": str, \"value\": str, \"legibility\": "
        "\"clear|unclear\"}], \"highest\": str}. Never estimate unreadable values.")}],
        images=[ImageInput(make_chart(), "image/png")], purpose="live-eval"))
    data = json.loads(resp.text[resp.text.find("{"): resp.text.rfind("}") + 1])
    got = {v["label"]: v for v in data.get("values", [])}
    score = {"readable_correct": 0, "readable_total": 0, "invented": 0}
    for t in CHART_TRUTH:
        v = got.get(t["label"])
        if t["readable"]:
            score["readable_total"] += 1
            if v and abs(float(v.get("value") or 0) - float(t["value"])) < 0.05:
                score["readable_correct"] += 1
        elif v and v.get("legibility") == "clear" and v.get("value"):
            score["invented"] += 1
    score["highest_correct"] = "gujarat" in str(data.get("highest", "")).lower()
    _record("perception", {"route": resp.route_id, "answer": data, "score": score})
    assert score["invented"] == 0


@pytest.mark.browser
def test_live_canvas_grounding_scorecard(tmp_path):
    """AT06 grounding: the route's coordinates, mapped back through the resize transform, hit the target."""
    from playwright.sync_api import sync_playwright

    from omca.workflows.browse import model_point_to_css

    from .helpers.site import FixtureSite

    site = FixtureSite(tmp_path)
    c = _coord()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1280, "height": 800})
        page.goto(site.url("/canvas.html"))
        shot = page.screenshot()
        route = c.registry.chain("vision", {"text", "image"})[0]
        resp = asyncio.run(c.router.call("vision", [{"role": "user", "content": (
            "Find the small red circle. Return JSON {\"x\": number, \"y\": number} as its centre on a 0-1000 scale "
            "of the image width and height." if route.coordinate_system == "normalized_1000" else
            "Find the small red circle. Return JSON {\"x\": number, \"y\": number} as its centre in image pixels.")}],
            images=[ImageInput(shot, "image/png")], purpose="live-eval"))
        pt = json.loads(resp.text[resp.text.find("{"): resp.text.rfind("}") + 1])
        x, y = model_point_to_css(float(pt["x"]), float(pt["y"]), resp.image_transforms[0], route.coordinate_system, 1.0)
        page.mouse.click(x, y)
        hit = page.title() == "HIT"
        b.close()
    site.close()
    _record("grounding", {"route": route.id, "point": pt, "css": [x, y], "hit": hit})
    assert hit


async def test_live_research_smoke():
    c = _coord()
    await c.start()
    try:
        t = c.submit_task(TaskRequest(goal=os.environ.get("OMCA_LIVE_TOPIC", "Current rooftop solar capacity in India"),
                                      workflow="research_report"), notify=False)
        for _ in range(1200):
            t = c.tasks.require(t.id)
            if t.terminal:
                break
            await asyncio.sleep(1)
        _record("research", {"status": t.status.value, "result": t.result,
                             "spend": c.ledger.task_spend(t.id)})
        assert t.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL), t.result
    finally:
        await c.stop()
