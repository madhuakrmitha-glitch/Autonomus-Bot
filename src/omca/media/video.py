"""Video and audio processing primitives (spec section 12; FFmpeg S15, faster-whisper S16).

Pure functions over local files, usable on the coordinator host or a worker. Frame
extraction and transcription are kept distinct: a transcript never stands in for visual
analysis, and sampling never claims every frame was inspected.
"""

from __future__ import annotations

import io
import json
import math
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class MediaError(Exception):
    pass


@dataclass
class MediaInfo:
    duration: float | None
    width: int | None
    height: int | None
    has_audio: bool
    has_video: bool
    is_live: bool = False


@dataclass
class Segment:
    id: str
    start: float
    end: float
    text: str
    words: list[dict[str, Any]] | None = None


def _run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{cmd[0]} timed out") from exc


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    res = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    if res.returncode != 0:
        raise MediaError(f"ffprobe could not read the media: {res.stderr.decode('utf-8', 'replace')[:200]}")
    data = json.loads(res.stdout or b"{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = data.get("format", {}).get("duration") or (v or {}).get("duration")
    duration = float(dur) if dur not in (None, "N/A") else None
    return MediaInfo(duration=duration, width=int(v["width"]) if v and v.get("width") else None,
                     height=int(v["height"]) if v and v.get("height") else None, has_audio=a is not None,
                     has_video=v is not None, is_live=duration is None)


def extract_frame(path: Path, t: float, max_side: int = 1024, ffmpeg: str = "ffmpeg") -> bytes:
    scale = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    res = _run([ffmpeg, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1", "-vf", scale,
                "-f", "image2", "-c:v", "png", "pipe:1"], 120)
    if res.returncode != 0 or not res.stdout:
        raise MediaError(f"could not decode a frame at {t:.2f}s: {res.stderr.decode('utf-8', 'replace')[:200]}")
    return res.stdout


def scene_changes(path: Path, threshold: float = 0.25, max_changes: int = 200, ffmpeg: str = "ffmpeg",
                  start: float | None = None, end: float | None = None) -> list[tuple[float, float]]:
    """Timestamps (and scores) where the picture changes noticeably (FFmpeg scene detection)."""
    cmd = [ffmpeg, "-v", "info", "-nostats"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None and start is not None:
        cmd += ["-t", f"{max(0.0, end - start):.3f}"]
    cmd += ["-i", str(path), "-an", "-vf", f"select='gt(scene,{threshold})',metadata=print", "-f", "null", "-"]
    res = _run(cmd, 900)
    out = []
    text = res.stderr.decode("utf-8", "replace")
    times = re.findall(r"pts_time:([0-9.]+)", text)
    scores = re.findall(r"lavfi\.scene_score=([0-9.]+)", text)
    for i, t in enumerate(times[:max_changes]):
        out.append((float(t) + (start or 0.0), float(scores[i]) if i < len(scores) else threshold))
    return out


def frame_difference(a: bytes, b: bytes) -> float:
    """Mean absolute grey-level difference (0-1) between two frames on a small common grid."""
    from PIL import Image, ImageChops, ImageStat

    with Image.open(io.BytesIO(a)) as ia, Image.open(io.BytesIO(b)) as ib:
        ga = ia.convert("L").resize((96, 54))
        gb = ib.convert("L").resize((96, 54))
        diff = ImageChops.difference(ga, gb)
        return ImageStat.Stat(diff).mean[0] / 255.0


def extract_audio(path: Path, out_wav: Path, ffmpeg: str = "ffmpeg", start: float | None = None,
                  duration: float | None = None) -> Path:
    cmd = [ffmpeg, "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_wav)]
    res = _run(cmd, 900)
    if res.returncode != 0:
        raise MediaError(f"audio extraction failed: {res.stderr.decode('utf-8', 'replace')[:200]}")
    return out_wav


def audio_is_silent(wav: Path, ffmpeg: str = "ffmpeg", threshold_db: float = -50.0) -> bool:
    res = _run([ffmpeg, "-v", "info", "-nostats", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"], 300)
    m = re.search(r"max_volume:\s*(-?[0-9.]+|-inf) dB", res.stderr.decode("utf-8", "replace"))
    if not m:
        return False
    return m.group(1) == "-inf" or float(m.group(1)) < threshold_db


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(s)
    h, mi, se, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000.0


def parse_captions(text: str) -> list[Segment]:
    """Parse WebVTT or SRT captions into segments (tags stripped, rolling duplicates merged)."""
    segs: list[Segment] = []
    blocks = re.split(r"\r?\n\r?\n", text.replace("﻿", ""))
    for block in blocks:
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None:
            continue
        a, b = lines[timing].split("-->", 1)
        try:
            start, end = _ts(a), _ts(b.split()[0] if b.split() else b)
        except ValueError:
            continue
        body = " ".join(re.sub(r"<[^>]+>", "", ln).strip() for ln in lines[timing + 1:])
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue
        if segs and (body == segs[-1].text or segs[-1].text.endswith(body)):
            segs[-1].end = max(segs[-1].end, end)
            continue
        segs.append(Segment(id=f"cap-{len(segs)}", start=start, end=end, text=body))
    return segs


def transcribe(wav: Path, model: str = "small", device: str = "auto", compute_type: str = "int8",
               offset: float = 0.0) -> tuple[list[Segment], dict[str, Any]]:
    """faster-whisper transcription with word timestamps and VAD (no speech -> no segments)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MediaError("faster-whisper is not installed (pip install omca[video])") from exc
    try:
        wm = WhisperModel(model, device=device, compute_type=compute_type)
    except Exception as exc:  # noqa: BLE001 - model download or device problems
        raise MediaError(f"could not load the Whisper model {model}: {exc}") from exc
    segments, info = wm.transcribe(str(wav), vad_filter=True, word_timestamps=True, beam_size=1)
    out = []
    for i, s in enumerate(segments):
        text = (s.text or "").strip()
        if not text:
            continue
        words = [{"start": w.start + offset, "end": w.end + offset, "word": w.word} for w in (s.words or [])]
        out.append(Segment(id=f"asr-{i}", start=s.start + offset, end=s.end + offset, text=text, words=words))
    meta = {"engine": "faster-whisper", "model": model, "language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None)}
    return out, meta


def coarse_timestamps(start: float, end: float, max_frames: int) -> tuple[list[float], float]:
    span = max(0.0, end - start)
    if span <= 0:
        return [start], 0.0
    n = max(1, min(max_frames, int(math.ceil(span / 2.0))))
    step = span / n
    return [round(start + step * (k + 0.5), 3) for k in range(n)], step


def dense_timestamps(start: float, end: float, max_frames: int, min_step: float = 0.5) -> list[float]:
    span = max(0.0, end - start)
    n = max(1, min(max_frames, int(span / min_step) or 1))
    step = span / n if n else span
    return [round(start + step * (k + 0.5), 3) for k in range(n)]


def segments_in(segs: list[Segment], start: float, end: float) -> list[Segment]:
    return [s for s in segs if s.end > start and s.start < end]


def segments_to_json(segs: list[Segment]) -> str:
    return json.dumps([asdict(s) for s in segs])


def segments_from_json(text: str) -> list[Segment]:
    return [Segment(**s) for s in json.loads(text)]


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None
