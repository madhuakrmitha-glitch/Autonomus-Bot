"""AT07, AT08 (task logic), AT09 and FR16 evidence-gap reporting on the fixture site."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omca.contracts import TaskRequest, TaskStatus
from omca.evidence import locate_excerpt, numbers_in
from omca.reports import docx_text
from omca.research.search import StaticSearchAdapter


def search_fixture(site):
    return StaticSearchAdapter("fixture-search", lambda q, n: [
        {"title": "Rooftop solar adoption", "url": site.url("/research/solar.html")},
        {"title": "Grid integration", "url": site.url("/research/grid.html")},
        {"title": "Unavailable", "url": site.url("/down/page.html")},
    ])


@pytest.mark.acceptance("AT09")
async def test_word_report_end_to_end(make_system, site, brain):
    s = await make_system()
    s.coord.search.search_adapter = search_fixture(site)
    task = s.coord.submit_task(TaskRequest(goal="Report on rooftop solar capacity in India", workflow="research_report"))
    t = await s.wait(task.id, timeout=120)
    assert t.status == TaskStatus.COMPLETED, (t.result, t.error_code)
    checks = t.result["details"]["checks"]
    assert all(checks.values()), checks

    # the saved report reopens, has the stored checksum and was delivered with that checksum
    reports = s.coord.artifacts.for_task(t.id, "report")
    assert len(reports) == 1
    manifest = reports[0]
    data = s.coord.artifacts.read_bytes(manifest.artifact_id)
    assert manifest.validation["ok"] is True
    saved_path = Path(t.result["summary"].split("Saved to: ")[1].strip())
    assert saved_path.exists() and saved_path.read_bytes() == data
    docs = s.channel.documents()
    assert docs and docs[-1].data == data and manifest.sha256[:16] in docs[-1].text

    # compare stated claims against evidence, not just citation presence
    text = docx_text(data)
    evidence = {e.evidence_id: e for e in s.coord.evidence.for_task(t.id)}
    sources = {}
    for c in s.coord.evidence.claims(t.id):
        assert c.text in text
        if c.support != "supported":
            continue
        assert c.evidence_ids, c
        for eid in c.evidence_ids:
            ev = evidence[eid]
            assert ev.source_url.startswith(site.base)
            # each supported claim's numbers appear in its cited excerpt
            assert numbers_in(c.text) <= numbers_in(ev.excerpt or "") | {"2026"}, (c.text, ev.excerpt)
            sources.setdefault(ev.source_url, []).append(ev)
    # every excerpt really occurs in the source page
    for url, evs in sources.items():
        html = (site.root / url.replace(site.base + "/", "")).read_text()
        for ev in evs:
            assert locate_excerpt(re.sub("<[^>]+>", " ", html), ev.excerpt), ev.excerpt
    # unsupported statements are marked, and the fabricated quote was dropped
    assert "unsupported - no cited evidence" in text
    assert "This sentence does not appear anywhere" not in text
    # the unavailable source is reported honestly as an evidence gap (FR16)
    assert any("evidence gap" in lim and "/down/" in lim for lim in t.result["limitations"])
    # the disagreement between sources is reported rather than resolved silently
    assert "Where sources disagree" in text


@pytest.mark.acceptance("AT07")
async def test_chart_values_confirmed_and_unreadable_not_invented(make_system, site, brain):
    brain.hallucinate_unreadable = True  # the model "reads" the unreadable bar in the first pass
    s = await make_system()
    s.coord.search.search_adapter = search_fixture(site)
    task = s.coord.submit_task(TaskRequest(goal="Report on rooftop solar capacity by state, including the chart",
                                           workflow="research_report"))
    t = await s.wait(task.id, timeout=120)
    assert t.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL), t.result
    # actual image pixels reached the vision route
    assert any(c["images"] >= 1 and "analyse images" in c["system"] for c in brain.calls)
    img_ev = [e for e in s.coord.evidence.for_task(t.id) if e.kind == "image_region"]
    assert img_ev, "chart evidence missing"
    confirmed = img_ev[0].locator["confirmed_values"]
    assert "Gujarat: 4.2" in confirmed and "Maharashtra: 3.6" in confirmed
    assert not any("Rajasthan" in v for v in confirmed)
    text = docx_text(s.coord.artifacts.read_bytes(s.coord.artifacts.for_task(t.id, "report")[0].artifact_id))
    assert "3.1" not in text  # the invented value never reaches the report
    assert any("Rajasthan" in lim for lim in t.result["limitations"])
    # the relationship is cited to the image evidence
    assert "Gujarat has a higher capacity than Maharashtra" in text


async def test_invented_number_is_flagged(make_system, site, brain):
    brain.invent_number = True
    s = await make_system()
    s.coord.search.search_adapter = search_fixture(site)
    task = s.coord.submit_task(TaskRequest(goal="Report on rooftop solar capacity in India", workflow="research_report"))
    t = await s.wait(task.id, timeout=120)
    claim = next(c for c in s.coord.evidence.claims(t.id) if "99 gigawatts" in c.text)
    assert claim.support == "uncertain" and "99" in (claim.review_note or "")
