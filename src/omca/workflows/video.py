"""Website video analysis (spec section 12, figure 5; FR17-FR19, NFR08).

Access: an accessible media download (yt-dlp) or, when that is unavailable, seeking the
authorized browser's playback and capturing decoded frames. Speech: captions when
available, otherwise faster-whisper; silence is reported as "no narration", never invented.
Vision: ordered, timestamped frame batches go to an open multimodal model, coarse first,
then denser sampling around scene changes and intervals the model flags. Findings become
video-interval evidence with frame and transcript references. Coverage is disclosed.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from ..contracts import VideoInterval
from ..engine import TaskBlocked, WorkflowOutcome
from ..media import video as mv
from ..models.types import ImageInput
from ..util import now_iso, truncate
from .common import finalize_report, model_json, model_review, review_claims, system_prompt, untrusted

URL = re.compile(r"https?://[^\s<>\"']+")
NARRATION_WORDS = re.compile(r"\b(narrat\w*|voice-?over|the speaker|speaker says|says that|explains that|mentions|"
                             r"is heard|we hear|spoken)\b", re.I)


def fmt(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


async def video_analysis(ctx) -> WorkflowOutcome:
    c, vc = ctx.c, ctx.config.video
    req = ctx.task.request
    url = req.params.get("url") or (URL.search(ctx.task.goal).group(0) if URL.search(ctx.task.goal) else None)
    if not url:
        answer = await ctx.ask_owner("Which video should I analyse? Send its URL.", key="url")
        m = URL.search(answer or "")
        if not m:
            return WorkflowOutcome("FAILED", "No video URL was provided.", error_code="EVIDENCE_GAP")
        url = m.group(0)
    question = str(req.params.get("question") or ctx.task.goal)

    # 1. access -----------------------------------------------------------------------------------------------
    ctx.progress("retrieving the video", phase="access")
    source, media_path, subtitle_ids, meta, playback_att = None, None, [], {}, None
    fetched = None
    try:
        fetched = await ctx.act("media.fetch", {"url": url, "max_height": vc.download_max_height,
                                                "max_filesize_mb": vc.download_max_filesize_mb}, key="fetch")
    except TaskBlocked as exc:
        ctx.note_limitation(f"no worker could download media ({exc})")
    if fetched is not None and fetched.ok:
        source = "download"
        media_path = c.artifacts.path(fetched.result["media_artifact_id"])
        subtitle_ids = fetched.result.get("subtitle_artifact_ids") or []
        meta = fetched.result
    else:
        access_error = (fetched.message if fetched else None) or "download unavailable"
        att = c.default_attachment(req.worker_id)
        if att is not None:
            probe_out = await ctx.step("playback-probe", lambda: _playback_probe(ctx, url, att.id))
            if probe_out.get("ok"):
                source, playback_att = "playback", att.id
                meta = {"duration": probe_out.get("duration"), "title": probe_out.get("title")}
            else:
                access_error += f"; browser playback: {probe_out.get('error')}"
        if source is None:
            return WorkflowOutcome(
                "FAILED",
                f"I could not access the video at {url}, so no analysis was produced. Reason: {access_error}.",
                limitations=[f"inaccessible or protected media: {truncate(access_error, 300)}"],
                error_code=(fetched.error_code if fetched and fetched.error_code else "UNSUPPORTED_MEDIA"))

    # 2. bounds -----------------------------------------------------------------------------------------------
    info = None
    if source == "download":
        info = await ctx.step("probe", lambda: _probe(media_path, ctx))
    duration = (info or {}).get("duration") or meta.get("duration")
    interval = req.params.get("interval")
    start = float(interval[0]) if interval else 0.0
    if duration is None or (info or {}).get("is_live"):
        if not interval:
            ctx.note_limitation(f"live or unknown-length media: only {vc.live_max_seconds:.0f}s were observed")
        end = float(interval[1]) if interval else start + vc.live_max_seconds
    else:
        end = min(float(interval[1]) if interval else float(duration), float(duration))
    if end - start > vc.max_duration_seconds:
        ctx.note_limitation(f"only {fmt(start)}-{fmt(start + vc.max_duration_seconds)} of {fmt(end)} was analysed "
                            f"(limit {vc.max_duration_seconds / 60:.0f} min per task)")
        end = start + vc.max_duration_seconds
    if end <= start:
        return WorkflowOutcome("FAILED", "The video has no analysable duration.", error_code="UNSUPPORTED_MEDIA")

    # 3. transcript (distinct from visual analysis) --------------------------------------------------------------
    ctx.progress("preparing the transcript", phase="transcript")
    tr = await ctx.step("transcript", lambda: _transcript(ctx, source, media_path, subtitle_ids, info, start, end))
    segments = [mv.Segment(**s) for s in tr["segments"]]
    for lim in tr["limitations"]:
        ctx.note_limitation(lim)
    has_narration = bool(segments)

    # 4-6. coarse sampling per segment, then denser inspection ---------------------------------------------------
    spans = []
    seg_len = max(30.0, vc.segment_seconds)
    s0 = start
    while s0 < end:
        spans.append((s0, min(end, s0 + seg_len)))
        s0 += seg_len
    per_span = max(4, vc.max_coarse_frames // max(1, len(spans)))
    frames_total = 0
    observations: list[dict[str, Any]] = []
    flagged: list[tuple[float, float, str]] = []
    coverage = []
    for si, (a, b) in enumerate(spans):
        ctx.progress(f"sampling {fmt(a)}-{fmt(b)}", phase="frames")
        coarse = await ctx.step(f"coarse-frames:{si}", lambda a=a, b=b, si=si: _sample(
            ctx, source, media_path, playback_att, mv.coarse_timestamps(a, b, per_span)[0], key=f"cf{si}"))
        frames_total += len(coarse["frames"])
        step = (b - a) / max(1, len(coarse["frames"]))
        coverage.append(f"{fmt(a)}-{fmt(b)}: {len(coarse['frames'])} frames, about every {step:.1f}s")
        cuts = await ctx.step(f"scenes:{si}", lambda a=a, b=b: _scenes(ctx, source, media_path, a, b))
        for t in cuts["changes"]:
            flagged.append((max(a, t - 1.0), min(b, t + 1.0), "scene change"))
        for prev, nxt in zip(coarse["frames"], coarse["frames"][1:], strict=False):
            if nxt.get("diff", 0) >= 0.18:
                flagged.append((prev["t"], nxt["t"], "large visual change between samples"))
        batches = [coarse["frames"][k: k + vc.frames_per_model_call]
                   for k in range(0, len(coarse["frames"]), vc.frames_per_model_call)]
        for bi, batch in enumerate(batches):
            res = await _analyse_batch(ctx, batch, segments, question, has_narration, key=f"coarse:{si}:{bi}")
            observations.extend(res.get("observations", []))
            for ev in res.get("events", []):
                if ev.get("needs_closer_look"):
                    flagged.append((float(ev.get("start", batch[0]["t"])), float(ev.get("end", batch[-1]["t"])),
                                    str(ev.get("description") or "model flagged")))

    intervals = _merge(flagged, start, end)
    budget = max(0, vc.max_total_frames - frames_total)
    inspected, skipped = 0, 0
    for ii, (a, b, why) in enumerate(intervals):
        n = min(vc.max_dense_frames_per_interval, budget)
        if n <= 0 or inspected >= 8:
            skipped += 1
            continue
        ctx.progress(f"inspecting {fmt(a)}-{fmt(b)} more closely ({why})", phase="dense")
        dense = await ctx.step(f"dense-frames:{ii}", lambda a=a, b=b, n=n, ii=ii: _sample(
            ctx, source, media_path, playback_att, mv.dense_timestamps(a, b, n), key=f"df{ii}"))
        budget -= len(dense["frames"])
        frames_total += len(dense["frames"])
        inspected += 1
        coverage.append(f"dense {fmt(a)}-{fmt(b)} ({why}): {len(dense['frames'])} frames")
        res = await _analyse_batch(ctx, dense["frames"][: max(1, vc.frames_per_model_call * 2)], segments, question,
                                   has_narration, key=f"dense:{ii}", dense=True)
        observations.extend(res.get("observations", []))
    if skipped:
        ctx.note_limitation(f"{skipped} candidate interval(s) with fast changes were not inspected densely (frame budget)")
    ctx.note_limitation("coverage: frames were sampled, not every frame was inspected; events shorter than the sampling "
                        "interval between samples may be missed. " + "; ".join(coverage[:12]))

    # 7. evidence ---------------------------------------------------------------------------------------------------
    findings = await ctx.step("evidence", lambda: _evidence(ctx, url, observations, segments, tr["provenance"]))
    if not findings["findings"]:
        return WorkflowOutcome("PARTIAL", "Frames were sampled but the model reported no relevant visual findings.",
                               limitations=list(ctx.limitations), error_code="EVIDENCE_GAP")

    # 8. synthesis, review, report ----------------------------------------------------------------------------------
    ctx.progress("writing the timestamped report", phase="synthesise")
    evidence = {e.evidence_id: e for e in c.evidence.for_task(ctx.task_id)}
    listing = "\n".join(f"[{f['evidence_id']}] {f['statement']}" + (f" | transcript: {truncate(f['quote'], 200)}" if f.get("quote") else "")
                        for f in findings["findings"])
    narration_rule = ("The video has a transcript; attribute speech only to transcript excerpts." if has_narration else
                      "There is NO narration or transcript for this video. Do not describe anything as being said, "
                      "narrated or explained by a speaker.")
    synth = await model_json(ctx, "summarisation", [
        {"role": "system", "content": system_prompt(ctx, "You write timestamped video analysis reports from evidence. "
                                                         "Every statement starts with its timestamp range and cites "
                                                         "evidence ids. " + narration_rule)},
        {"role": "user", "content": f"Question: {question}\n\nFindings:\n" + untrusted("video findings", listing, 30000)
         + "\n\nReturn JSON {\"title\": str, \"summary\": [{\"text\": str, \"evidence_ids\": [ids]}], \"sections\": "
           "[{\"heading\": str, \"statements\": [{\"text\": str, \"evidence_ids\": [ids]}]}]}"},
    ], key="synthesise", max_output_tokens=4096, default={})
    synth = synth if isinstance(synth, dict) else {}
    summary = review_claims(list(synth.get("summary") or []), evidence)
    sections = [(str(s.get("heading") or "Timeline"), review_claims(list(s.get("statements") or []), evidence))
                for s in synth.get("sections") or [] if isinstance(s, dict)]
    if not summary and not sections:
        sections = [("Timeline", review_claims([{"text": f["statement"], "evidence_ids": [f["evidence_id"]]}
                                                for f in findings["findings"][:30]], evidence))]
    stmts = summary + [x for _, st in sections for x in st]
    if not has_narration:
        for s in stmts:
            if NARRATION_WORDS.search(s["text"]):
                s["support"], s["note"] = "unsupported", "the video has no narration; speech cannot be attributed"
    await model_review(ctx, stmts, evidence)
    method = {
        "Access": "accessible media download (yt-dlp)" if source == "download" else "authorized browser playback capture",
        "Transcript": tr["provenance"],
        "Frames inspected": f"{frames_total} (coarse sampling plus denser sampling of {inspected} interval(s))",
        "Visual model route": ", ".join(r.id for r in _routes(c)),
        "Native video input": "not used (portable timestamped frames and transcript pipeline)",
    }
    title = str(synth.get("title") or f"Video analysis: {truncate(meta.get('title') or url, 80)}")
    built, saved, delivered, checks = await finalize_report(
        ctx, title=title, summary=summary, sections=sections, figures=None, disagreements=[],
        limitations=list(ctx.limitations), method=method, deliver_caption=f"Video report: {title}")
    ok = all(checks.values())
    return WorkflowOutcome("COMPLETED" if ok else "PARTIAL",
                           f"{title}\n{truncate(' '.join(s['text'] for s in summary[:3]), 900)}",
                           artifacts=[built["artifact_id"]], limitations=list(ctx.limitations),
                           details={"checks": checks, "frames": frames_total, "transcript": tr["provenance"],
                                    "source": source, "intervals_inspected": inspected},
                           checks_passed=ok)


def _routes(c):
    try:
        return c.registry.chain("vision", {"text", "image"})[:1]
    except Exception:  # noqa: BLE001
        return []


async def _probe(media_path: Path, ctx) -> dict[str, Any]:
    info = mv.probe(media_path)
    return {"duration": info.duration, "width": info.width, "height": info.height, "has_audio": info.has_audio,
            "has_video": info.has_video, "is_live": info.is_live}


async def _playback_probe(ctx, url: str, att_id: str) -> dict[str, Any]:
    try:
        nav = await ctx.act("browser.navigate", {"url": url}, key="pb:nav", attachment_id=att_id)
    except TaskBlocked as exc:
        return {"ok": False, "error": str(exc)}
    if not nav.ok:
        return {"ok": False, "error": nav.message or nav.reason}
    shot = await ctx.act("browser.video_frames", {"timestamps": [0.5]}, key="pb:probe", attachment_id=att_id)
    if not shot.ok:
        return {"ok": False, "error": shot.message or "no playable video element"}
    return {"ok": True, "duration": shot.result.get("duration"), "title": nav.obs.title if nav.obs else None}


async def _transcript(ctx, source, media_path, subtitle_ids, info, start, end) -> dict[str, Any]:
    c = ctx.c
    lims: list[str] = []
    segs: list[mv.Segment] = []
    provenance = "none"
    if subtitle_ids:
        text = c.artifacts.read_bytes(subtitle_ids[0]).decode("utf-8", "replace")
        segs = [s for s in mv.parse_captions(text) if s.end > start and s.start < end]
        provenance = f"captions file {c.artifacts.get(subtitle_ids[0]).name} (segment timestamps)"
    elif source == "download" and (info or {}).get("has_audio"):
        sp = ctx.config.models.speech
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "audio.wav"
            try:
                mv.extract_audio(media_path, wav, start=start, duration=end - start)
                if mv.audio_is_silent(wav):
                    lims.append("the audio track is silent: no narration")
                    provenance = "audio present but silent"
                elif sp.engine == "disabled":
                    lims.append("speech recognition is disabled; speech was not transcribed")
                else:
                    segs, meta = mv.transcribe(wav, sp.model, sp.device, sp.compute_type, offset=start)
                    provenance = f"faster-whisper {sp.model} (word timestamps; language {meta.get('language')})"
                    if not segs:
                        lims.append("no speech was detected in the audio")
            except mv.MediaError as exc:
                lims.append(f"speech was not transcribed: {exc}")
    elif source == "playback":
        lims.append("browser playback capture is visual only; audio was not captured")
    else:
        lims.append("the media has no audio track: no narration")
    if segs:
        c.artifacts.put(ctx.task_id, "media", "transcript.json", mv.segments_to_json(segs).encode("utf-8"),
                        source_map={"provenance": provenance})
    return {"segments": [s.__dict__ for s in segs], "provenance": provenance, "limitations": lims}


async def _sample(ctx, source, media_path, att_id, timestamps: list[float], *, key: str) -> dict[str, Any]:
    c, vc = ctx.c, ctx.config.video
    frames: list[dict[str, Any]] = []
    if source == "download":
        prev = None
        for t in timestamps:
            try:
                png = mv.extract_frame(media_path, t, vc.frame_max_side)
            except mv.MediaError as exc:
                ctx.note_limitation(f"frame at {fmt(t)} could not be decoded: {exc}")
                continue
            stored = c.artifacts.put_image(ctx.task_id, "media", f"frame_{t:09.3f}.png", png,
                                           source_map={"timestamp": t, "method": "ffmpeg"})
            diff = mv.frame_difference(prev, png) if prev else 0.0
            prev = png
            frames.append({"t": t, "artifact_id": stored.manifest.artifact_id, "diff": round(diff, 4)})
    else:
        for k in range(0, len(timestamps), 16):
            out = await ctx.act("browser.video_frames", {"timestamps": timestamps[k: k + 16], "max_side": vc.frame_max_side},
                                key=f"{key}:{k}", attachment_id=att_id)
            if not out.ok:
                ctx.note_limitation(f"playback capture failed near {fmt(timestamps[k])}: {out.message}")
                continue
            prev_id = None
            for f in out.result.get("frames", []):
                diff = 0.0
                if prev_id:
                    diff = mv.frame_difference(c.artifacts.read_bytes(prev_id), c.artifacts.read_bytes(f["artifact_id"]))
                prev_id = f["artifact_id"]
                frames.append({"t": float(f["timestamp"]), "artifact_id": f["artifact_id"], "diff": round(diff, 4)})
    return {"frames": frames}


async def _scenes(ctx, source, media_path, a: float, b: float) -> dict[str, Any]:
    if source != "download":
        return {"changes": []}
    try:
        return {"changes": [t for t, _ in mv.scene_changes(media_path, ctx.config.video.scene_threshold, 40, start=a, end=b)]}
    except mv.MediaError as exc:
        ctx.note_limitation(f"scene detection unavailable: {exc}")
        return {"changes": []}


async def _analyse_batch(ctx, frames: list[dict[str, Any]], segments, question: str, has_narration: bool, *, key: str,
                         dense: bool = False) -> dict[str, Any]:
    if not frames:
        return {}
    c = ctx.c
    a, b = frames[0]["t"], frames[-1]["t"]
    spoken = " ".join(f"[{fmt(s.start)}] {s.text}" for s in mv.segments_in(segments, a - 1, b + 1))
    images = [ImageInput(c.artifacts.read_bytes(f["artifact_id"]), "image/png", f["artifact_id"], label=f"Frame at {f['t']:.1f}s:")
              for f in frames]
    transcript_note = untrusted("transcript", spoken, 4000) if spoken else (
        "(no speech in this span)" if has_narration else "(this video has no narration)")
    data = await model_json(ctx, "vision", [
        {"role": "system", "content": system_prompt(ctx, "You analyse ordered video frames. Each image is labelled with "
                                                         "its timestamp. Describe only what is visible; speech comes "
                                                         "only from the transcript text.")},
        {"role": "user", "content": (
            f"Question: {question}\nFrames {fmt(a)}-{fmt(b)} ({'dense' if dense else 'coarse'} sampling).\n"
            f"Transcript for this span: {transcript_note}\n\nReturn JSON {{\"observations\": [{{\"t\": seconds, "
            "\"description\": str}}], \"events\": [{\"start\": seconds, \"end\": seconds, \"description\": str, "
            "\"needs_closer_look\": bool}]}. Set needs_closer_look for brief actions, fast changes or small text "
            "that the sampling may have missed.")},
    ], key=key, images=images, max_output_tokens=1500, default={})
    data = data if isinstance(data, dict) else {}
    frame_ids = {round(f["t"], 1): f["artifact_id"] for f in frames}
    obs = []
    for o in data.get("observations") or []:
        if not isinstance(o, dict) or not o.get("description"):
            continue
        try:
            t = float(o.get("t", a))
        except (TypeError, ValueError):
            t = a
        nearest = min(frames, key=lambda f: abs(f["t"] - t))
        obs.append({"t": nearest["t"], "description": str(o["description"]), "frame_id": frame_ids.get(round(nearest["t"], 1)),
                    "span": [a, b], "dense": dense})
    return {"observations": obs, "events": [e for e in data.get("events") or [] if isinstance(e, dict)]}


def _merge(flagged: list[tuple[float, float, str]], lo: float, hi: float) -> list[tuple[float, float, str]]:
    items = sorted((max(lo, a), min(hi, max(b, a + 1.0)), why) for a, b, why in flagged if b >= a)
    out: list[tuple[float, float, str]] = []
    for a, b, why in items:
        if out and a <= out[-1][1] + 0.5:
            pa, pb, pw = out[-1]
            out[-1] = (pa, max(pb, b), pw if why in pw else f"{pw}; {why}")
        else:
            out.append((a, b, why))
    return out


async def _evidence(ctx, url: str, observations: list[dict[str, Any]], segments, provenance: str) -> dict[str, Any]:
    c = ctx.c
    findings = []
    for k, o in enumerate(sorted(observations, key=lambda x: x["t"])):
        t = float(o["t"])
        a = b = t
        near = mv.segments_in(segments, a - 2, b + 2)
        quote = " ".join(s.text for s in near)[:280] if near else None
        ev = c.evidence.record(
            ctx.task_id, kind="video_interval", source_url=url, captured_at=now_iso(),
            content_for_hash=f"{url}|{t}|{o['description']}", excerpt=quote,
            video_interval=VideoInterval(start_seconds=a, end_seconds=b, frame_artifact_ids=[o["frame_id"]] if o.get("frame_id") else [],
                                         transcript_segment_ids=[s.id for s in near]),
            image_artifact_id=o.get("frame_id"), locator={"observation": o["description"], "frame_time": t},
            limitations=f"visual description by an open vision model from a sampled frame; transcript: {provenance}",
            provider="video", stable_key=f"video:{k}:{t}",
        )
        findings.append({"evidence_id": ev.evidence_id, "statement": f"[{fmt(t)}] {o['description']}", "quote": quote or "",
                         "t": t})
    return {"findings": findings}
