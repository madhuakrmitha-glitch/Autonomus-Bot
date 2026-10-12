"""Video acceptance cases AT16-AT20 on generated fixture videos (FR17-FR19, NFR08)."""

from __future__ import annotations

import pytest

from omca.contracts import TaskRequest, TaskStatus
from omca.media import video as mv
from omca.reports import docx_text

pytestmark = [pytest.mark.video, pytest.mark.browser]


def video_task(s, url: str, question: str = "What happens in the video?", **params):
    return s.coord.submit_task(TaskRequest(goal=f"Analyse {url}", workflow="video_analysis",
                                           params={"url": url, "question": question, **params}))


def report_text(s, task_id: str) -> str:
    return docx_text(s.coord.artifacts.read_bytes(s.coord.artifacts.for_task(task_id, "report")[0].artifact_id))


@pytest.mark.acceptance("AT16")
async def test_spoken_video_frames_and_transcript_support_timestamped_findings(make_system, site, brain):
    s = await make_system()
    t = video_task(s, site.url("/video/page.html"), "Describe the assembly steps.")
    t = await s.wait(t.id, timeout=180)
    assert t.status == TaskStatus.COMPLETED, (t.result["details"].get("checks"),
                                              s.coord.artifacts.for_task(t.id, "report")[0].validation.get("problems"))
    details = t.result["details"]
    assert details["source"] == "download" and details["transcript"].startswith("captions file")
    evidence = [e for e in s.coord.evidence.for_task(t.id) if e.kind == "video_interval"]
    assert evidence and all(e.video_interval.frame_artifact_ids for e in evidence)
    # a frame in the red scene (3-6 s) is linked to the caption spoken at that time
    red = [e for e in evidence if 3.0 <= e.video_interval.start_seconds < 6.0]
    assert red and any("red bracket" in (e.excerpt or "") for e in red)
    text = report_text(s, t.id)
    assert "[00:0" in text  # timestamped statements
    # actual frames (pixels) reached the vision route in timestamp order
    frame_calls = [c for c in brain.calls if "ordered video frames" in c["system"]]
    assert frame_calls and all(c["images"] >= 1 for c in frame_calls)


@pytest.mark.acceptance("AT17")
async def test_silent_demonstration_does_not_claim_narration(make_system, site, brain):
    brain.claim_narration = True  # the model tries to describe a narrator
    s = await make_system()
    t = video_task(s, site.url("/video/silent.mp4"), "What does the demonstration show?")
    t = await s.wait(t.id, timeout=180)
    assert t.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL), t.result
    assert any("no audio track" in lim or "no narration" in lim for lim in t.result["limitations"])
    claim = next(c for c in s.coord.evidence.claims(t.id) if "narrator" in c.text.lower())
    assert claim.support == "unsupported"
    assert all(not e.excerpt for e in s.coord.evidence.for_task(t.id))  # no transcript excerpts were invented


@pytest.mark.acceptance("AT18")
async def test_brief_event_triggers_denser_inspection(make_system, site, brain):
    s = await make_system()  # 4 coarse frames over 12 s (every 3 s) cannot see the 0.4 s flash at 7.0 s
    t = video_task(s, site.url("/video/silent.mp4"), "Does anything flash on screen, and when?")
    t = await s.wait(t.id, timeout=180)
    dense = [e for e in s.coord.tasks.events(t.id) if e["kind"] == "progress" and e.get("phase") == "dense"]
    assert dense, "no denser inspection happened"
    found = [c for c in s.coord.evidence.claims(t.id) if "white flash" in c.text.lower()]
    assert found or any("not inspected densely" in lim for lim in t.result["limitations"])
    if found:
        ts = [e.video_interval.start_seconds for e in s.coord.evidence.for_task(t.id)
              if e.evidence_id in found[0].evidence_ids]
        assert ts and all(6.5 <= x <= 7.6 for x in ts), ts
    assert any("not every frame was inspected" in lim for lim in t.result["limitations"])


@pytest.mark.acceptance("AT19")
async def test_inaccessible_media_returns_a_truthful_failure(make_system, site, brain):
    s = await make_system()
    t = video_task(s, site.url("/protected/lecture.mp4"))
    t = await s.wait(t.id, timeout=120)
    assert t.status == TaskStatus.FAILED
    assert "could not access the video" in t.result["summary"] and "no analysis was produced" in t.result["summary"]
    assert s.coord.artifacts.for_task(t.id, "report") == []
    assert not any("ordered video frames" in c["system"] for c in brain.calls)  # nothing was "analysed"


@pytest.mark.acceptance("AT20")
async def test_long_video_is_segmented_and_bounded(make_system, site, brain):
    s = await make_system(video={"max_duration_seconds": 8, "segment_seconds": 30, "max_coarse_frames": 6,
                                 "frames_per_model_call": 3, "max_dense_frames_per_interval": 3, "max_total_frames": 10})
    t = video_task(s, site.url("/video/silent.mp4"))
    t = await s.wait(t.id, timeout=180)
    assert t.status in (TaskStatus.COMPLETED, TaskStatus.PARTIAL), t.result
    assert any("only 00:00.0-00:08.0" in lim for lim in t.result["limitations"])
    assert t.result["details"]["frames"] <= 10
    for e in s.coord.evidence.for_task(t.id):
        assert 0.0 <= e.video_interval.start_seconds <= 8.0  # findings keep their timestamps inside the bound


def test_caption_parsing_merges_rolling_duplicates():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c>Hello</c> world\n\n00:00:02.000 --> 00:00:03.500\nHello world\n\n" \
          "00:00:04.000 --> 00:00:05.000\nNext line\n"
    segs = mv.parse_captions(vtt)
    assert [(s.start, s.end, s.text) for s in segs] == [(1.0, 3.5, "Hello world"), (4.0, 5.0, "Next line")]


def test_sampling_plans_are_bounded():
    ts, step = mv.coarse_timestamps(0, 3600, 48)
    assert len(ts) == 48 and step == 75.0
    assert len(mv.dense_timestamps(10, 20, 12)) == 12
