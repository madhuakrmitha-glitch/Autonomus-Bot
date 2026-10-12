"""Worker media handlers: accessible-media download with yt-dlp (reference S14).

yt-dlp is used only when the source permits retrieval; protected (DRM) or login-gated media
is reported as unavailable rather than worked around. Live streams need a finite interval,
which the browser playback route provides.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..contracts import ErrorCode, WorkerJob
from .agent import HandlerResult, JobContext, JobFailure

PROTECTED_HINTS = ("drm", "protected", "sign in", "login", "private video", "members-only", "premium", "403")


def _download(url: str, out_dir: Path, max_height: int, max_mb: int, want_subs: bool) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise JobFailure(ErrorCode.UNSUPPORTED_CAPABILITY, "yt-dlp is not installed on this worker") from exc
    opts: dict[str, Any] = {
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        "outtmpl": str(out_dir / "media.%(ext)s"),
        "max_filesize": max_mb * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 2,
        "writesubtitles": want_subs,
        "writeautomaticsub": want_subs,
        "subtitleslangs": ["en.*", "en"],
        "subtitlesformat": "vtt/srt/best",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            code = ErrorCode.AUTH_REQUIRED if any(h in msg for h in PROTECTED_HINTS) else ErrorCode.SITE_UNAVAILABLE
            raise JobFailure(code, f"media is not accessible for download: {str(exc)[:240]}") from exc
        if info.get("is_live"):
            raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, "live stream: a finite observation interval is required")
        if info.get("_type") == "playlist":
            raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, "playlists are not processed; send a single video URL")
        try:
            info = ydl.process_ie_result(info, download=True)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            code = ErrorCode.AUTH_REQUIRED if any(h in msg for h in PROTECTED_HINTS) else ErrorCode.SITE_UNAVAILABLE
            raise JobFailure(code, f"download failed: {str(exc)[:240]}") from exc
    files = [p for p in out_dir.iterdir() if p.is_file()]
    media = next((p for p in files if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".m4a", ".mp3")), None)
    if media is None:
        raise JobFailure(ErrorCode.UNSUPPORTED_MEDIA, "the download produced no media file (size limit or format)")
    subs = [p for p in files if p.suffix.lower() in (".vtt", ".srt")]
    return {"media": media, "subs": subs, "title": info.get("title"), "duration": info.get("duration"),
            "uploader": info.get("uploader"), "upload_date": info.get("upload_date"),
            "webpage_url": info.get("webpage_url") or url, "extractor": info.get("extractor")}


def make_media_handlers() -> dict[str, Any]:
    async def media_fetch(job: WorkerJob, ctx: JobContext) -> HandlerResult:
        a = job.arguments
        tmp = Path(tempfile.mkdtemp(prefix="omca-media-", dir=str(ctx.workspace)))
        try:
            info = await asyncio.to_thread(_download, a["url"], tmp, int(a.get("max_height", 720)),
                                           int(a.get("max_filesize_mb", 500)), bool(a.get("want_subtitles", True)))
            media_path: Path = info["media"]
            media_bytes = await asyncio.to_thread(media_path.read_bytes)
            art = await ctx.upload("media", media_path.name, media_bytes,
                                   meta={"source_url": a["url"], "title": info.get("title")})
            sub_ids = []
            for s in info["subs"][:2]:
                sub_ids.append(await ctx.upload("subtitles", s.name, s.read_bytes(), meta={"source_url": a["url"]}))
            return HandlerResult(
                result={"media_artifact_id": art, "subtitle_artifact_ids": sub_ids, "title": info.get("title"),
                        "duration": info.get("duration"), "uploader": info.get("uploader"),
                        "upload_date": info.get("upload_date"), "webpage_url": info.get("webpage_url"),
                        "extractor": info.get("extractor")},
                artifact_ids=[art, *sub_ids])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return {"media.fetch": media_fetch}
