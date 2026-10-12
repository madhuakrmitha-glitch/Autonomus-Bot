"""Artifact store: per-task workspace folders, atomic checksummed writes, lineage.

Layout (application layout, not the OpenClaw workspace format)::

    <workspace_root>/tasks/<task_id>/{inputs,observations,evidence,media,reports,outputs}/

Files are written to a temporary name and renamed into place, checksummed, and their
content type is sniffed independently of the filename.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .config import ApplicationConfig
from .contracts import ArtifactManifest, Rect
from .db import Database, dumps, loads
from .util import iso, new_id, now_iso, sha256_bytes, utcnow

KIND_DIRS = {
    "input": "inputs",
    "observation": "observations",
    "evidence": "evidence",
    "media": "media",
    "report": "reports",
    "output": "outputs",
    "transfer": "outputs",
}

# retention class per kind; None means "keep until the owner deletes it"
RETENTION = {
    "observation": "observation",
    "media": "media",
    "evidence": "keep",
    "report": "keep",
    "output": "keep",
    "input": "keep",
    "transfer": "keep",
}

EXT_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".vtt": "text/vtt",
    ".srt": "application/x-subrip",
    ".eml": "message/rfc822",
    ".ics": "text/calendar",
}

_TEXTUAL = {"application/json", "text/plain", "text/markdown", "text/csv", "text/html", "text/vtt",
            "application/x-subrip", "message/rfc822", "text/calendar"}


class ArtifactError(ValueError):
    pass


def sniff_media_type(data: bytes) -> str | None:
    head = data[:64]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "audio/mp4" if brand in (b"M4A ", b"M4B ") else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            return "application/zip"
        if "word/document.xml" in names:
            return EXT_MEDIA[".docx"]
        if "xl/workbook.xml" in names:
            return EXT_MEDIA[".xlsx"]
        if "ppt/presentation.xml" in names:
            return EXT_MEDIA[".pptx"]
        return "application/zip"
    try:
        text = data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return None
    stripped = text.lstrip()
    if stripped.startswith("WEBVTT"):
        return "text/vtt"
    if stripped.startswith(("{", "[")):
        return "application/json"
    if stripped[:15].lower().startswith(("<!doctype html", "<html")):
        return "text/html"
    if stripped.startswith("BEGIN:VCALENDAR"):
        return "text/calendar"
    return "text/plain"


def _compatible(declared: str, sniffed: str | None) -> bool:
    if sniffed is None:
        return False
    if declared == sniffed:
        return True
    if declared in _TEXTUAL and sniffed in _TEXTUAL | {"text/plain"}:
        return True
    if declared == "video/webm" and sniffed == "video/webm":
        return True
    if declared == "video/x-matroska" and sniffed == "video/webm":
        return True
    if declared in ("audio/mp4", "video/mp4") and sniffed in ("audio/mp4", "video/mp4"):
        return True
    return False


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, default: str = "file") -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = default
    stem, ext = os.path.splitext(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if stem.upper() in reserved:
        stem = f"_{stem}"
    return (stem[:120] or default) + ext[:16].lower()


@dataclass
class StoredArtifact:
    manifest: ArtifactManifest
    path: Path


class ArtifactStore:
    def __init__(self, db: Database, config: ApplicationConfig):
        self.db = db
        self.config = config
        self.root = Path(config.storage.workspace_root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths --------------------------------------------------------------
    def task_dir(self, task_id: str, kind: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_]+", task_id):
            raise ArtifactError("invalid task id")
        sub = KIND_DIRS.get(kind)
        if sub is None:
            raise ArtifactError(f"unknown artifact kind {kind}")
        return self.root / "tasks" / task_id / sub

    def abs_path(self, rel_path: str) -> Path:
        rel = PurePosixPath(rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ArtifactError("artifact paths must stay inside the workspace")
        p = (self.root / Path(*rel.parts)).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ArtifactError("artifact path escapes the workspace")
        return p

    def ensure_inside(self, path: Path, base: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(base.resolve()):
            raise ArtifactError(f"path {path} is outside approved directory {base}")
        return resolved

    # -- writing ------------------------------------------------------------
    def put(
        self,
        task_id: str | None,
        kind: str,
        name: str,
        data: bytes,
        *,
        media_type: str | None = None,
        parent_artifact_id: str | None = None,
        crop: Rect | None = None,
        width: int | None = None,
        height: int | None = None,
        resize_scale: float | None = None,
        source_map: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> StoredArtifact:
        fname = safe_filename(name)
        ext = os.path.splitext(fname)[1].lower()
        declared = media_type or EXT_MEDIA.get(ext)
        sniffed = sniff_media_type(data)
        if declared is None:
            raise ArtifactError(f"unsupported file type for {fname}")
        if not _compatible(declared, sniffed):
            raise ArtifactError(f"content of {fname} looks like {sniffed}, not {declared}")
        base = self.task_dir(task_id, kind) if task_id else self.root / "shared" / KIND_DIRS[kind]
        base.mkdir(parents=True, exist_ok=True)
        target = base / fname
        if target.exists() and not overwrite:
            target = self._unique(target)
        self.ensure_inside(target, self.root)
        self._atomic_write(target, data)
        rel = target.resolve().relative_to(self.root.resolve()).as_posix()
        artifact_id = new_id("art")
        retention = RETENTION.get(kind, "keep")
        expires_at = None
        if retention == "observation":
            expires_at = iso(utcnow() + timedelta(days=self.config.retention.observation_days))
        elif retention == "media":
            expires_at = iso(utcnow() + timedelta(days=self.config.retention.media_days))
        manifest = ArtifactManifest(
            artifact_id=artifact_id,
            task_id=task_id,
            name=target.name,
            rel_path=rel,
            media_type=declared,
            bytes=len(data),
            sha256=sha256_bytes(data),
            kind=kind,
            parent_artifact_id=parent_artifact_id,
            crop=crop,
            width=width,
            height=height,
            resize_scale=resize_scale,
            source_map=source_map or {},
            validation=validation or {},
            created_at=now_iso(),
        )
        with self.db.tx() as c:
            if overwrite:
                c.execute("UPDATE artifacts SET rel_path = rel_path || '#superseded:' || id WHERE rel_path=?", (rel,))
            c.execute(
                """INSERT INTO artifacts(id, task_id, name, rel_path, media_type, bytes, sha256, kind,
                   parent_artifact_id, crop_json, width, height, resize_scale, source_map_json, validation_json,
                   retention_class, expires_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id, task_id, manifest.name, rel, declared, len(data), manifest.sha256, kind,
                    parent_artifact_id, dumps(crop.model_dump()) if crop else None, width, height, resize_scale,
                    dumps(manifest.source_map), dumps(manifest.validation), retention, expires_at, manifest.created_at,
                ),
            )
        return StoredArtifact(manifest, target)

    def put_image(self, task_id: str | None, kind: str, name: str, data: bytes, **kw: Any) -> StoredArtifact:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        kw.setdefault("width", w)
        kw.setdefault("height", h)
        return self.put(task_id, kind, name, data, **kw)

    def crop(self, artifact_id: str, rect: Rect, *, name: str | None = None, kind: str = "evidence",
             upscale_to: int | None = None) -> StoredArtifact:
        """Derive a crop from an original image, keeping lineage and the crop rectangle."""
        from PIL import Image

        parent = self.get(artifact_id)
        if parent is None:
            raise ArtifactError(f"unknown artifact {artifact_id}")
        data = self.read_bytes(artifact_id)
        with Image.open(io.BytesIO(data)) as im:
            box = (
                max(0, int(rect.x)),
                max(0, int(rect.y)),
                min(im.width, int(rect.x + rect.width)),
                min(im.height, int(rect.y + rect.height)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ArtifactError("crop rectangle is outside the image")
            cropped = im.crop(box)
            scale = None
            if upscale_to and max(cropped.size) < upscale_to:
                scale = upscale_to / max(cropped.size)
                cropped = cropped.resize((round(cropped.width * scale), round(cropped.height * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            cropped.convert("RGB").save(buf, "PNG")
        real = Rect(x=box[0], y=box[1], width=box[2] - box[0], height=box[3] - box[1])
        return self.put_image(
            parent.task_id, kind, name or f"{parent.name.rsplit('.', 1)[0]}_crop.png", buf.getvalue(),
            parent_artifact_id=artifact_id, crop=real, resize_scale=scale,
        )

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    @staticmethod
    def _unique(target: Path) -> Path:
        stem, ext = target.stem, target.suffix
        for i in range(2, 10000):
            cand = target.with_name(f"{stem}-{i}{ext}")
            if not cand.exists():
                return cand
        raise ArtifactError("could not find a free file name")

    # -- reading ------------------------------------------------------------
    def get(self, artifact_id: str) -> ArtifactManifest | None:
        row = self.db.one("SELECT * FROM artifacts WHERE id=? AND deleted_at IS NULL", (artifact_id,))
        return _row_to_manifest(row) if row else None

    def path(self, artifact_id: str) -> Path:
        m = self.get(artifact_id)
        if m is None:
            raise ArtifactError(f"unknown artifact {artifact_id}")
        return self.abs_path(m.rel_path)

    def read_bytes(self, artifact_id: str, verify: bool = True) -> bytes:
        m = self.get(artifact_id)
        if m is None:
            raise ArtifactError(f"unknown artifact {artifact_id}")
        data = self.abs_path(m.rel_path).read_bytes()
        if verify and sha256_bytes(data) != m.sha256:
            raise ArtifactError(f"checksum mismatch for artifact {artifact_id}")
        return data

    def for_task(self, task_id: str, kind: str | None = None) -> list[ArtifactManifest]:
        if kind:
            rows = self.db.query(
                "SELECT * FROM artifacts WHERE task_id=? AND kind=? AND deleted_at IS NULL ORDER BY created_at", (task_id, kind)
            )
        else:
            rows = self.db.query("SELECT * FROM artifacts WHERE task_id=? AND deleted_at IS NULL ORDER BY created_at", (task_id,))
        return [_row_to_manifest(r) for r in rows]

    def set_validation(self, artifact_id: str, validation: dict[str, Any]) -> None:
        self.db.execute("UPDATE artifacts SET validation_json=? WHERE id=?", (dumps(validation), artifact_id))

    def keep(self, artifact_id: str) -> None:
        """Retain a reviewed evidence figure with its report."""
        self.db.execute("UPDATE artifacts SET retention_class='keep', expires_at=NULL WHERE id=?", (artifact_id,))

    def expire(self, now: str | None = None) -> int:
        now = now or now_iso()
        rows = self.db.query(
            "SELECT id, rel_path FROM artifacts WHERE expires_at IS NOT NULL AND expires_at < ? AND deleted_at IS NULL", (now,)
        )
        for r in rows:
            try:
                self.abs_path(r["rel_path"]).unlink(missing_ok=True)
            except (OSError, ArtifactError):
                pass
            self.db.execute("UPDATE artifacts SET deleted_at=? WHERE id=?", (now, r["id"]))
        return len(rows)


def _row_to_manifest(row: Any) -> ArtifactManifest:
    return ArtifactManifest(
        artifact_id=row["id"],
        task_id=row["task_id"],
        name=row["name"],
        rel_path=row["rel_path"],
        media_type=row["media_type"],
        bytes=row["bytes"],
        sha256=row["sha256"],
        kind=row["kind"],
        parent_artifact_id=row["parent_artifact_id"],
        crop=Rect(**loads(row["crop_json"])) if row["crop_json"] else None,
        width=row["width"],
        height=row["height"],
        resize_scale=row["resize_scale"],
        source_map=loads(row["source_map_json"], {}),
        validation=loads(row["validation_json"], {}),
        created_at=row["created_at"],
    )
