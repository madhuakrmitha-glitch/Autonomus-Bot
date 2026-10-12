"""Evidence ledger, claims and observation records (FR09, spec sections 10 and 16).

Each report claim references one or more evidence IDs. An evidence record keeps the source
URL, retrieval time, source date when available, and an excerpt, image region or video
interval. Excerpts are verified against the captured source text so a model cannot cite a
quote that is not in the source. Model-generated summaries are never stored as evidence.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .contracts import Claim, EvidenceRecord, Observation, Rect, VideoInterval
from .db import Database, dumps, loads
from .util import new_id, now_iso, sha256_text, truncate


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def locate_excerpt(source_text: str, excerpt: str) -> tuple[int, int] | None:
    """Find an excerpt in the source (whitespace/quote-insensitive). Returns normalized offsets."""
    src = normalize_text(source_text)
    ex = normalize_text(excerpt).strip(" .\"'")
    if len(ex) < 12:
        return None
    idx = src.find(ex)
    if idx == -1:
        return None
    return idx, idx + len(ex)


NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?")


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in NUMBER.finditer(text or ""):
        v = m.group(0).replace(",", "").rstrip("%").lstrip("+")
        if v in {"", "-"}:
            continue
        try:
            f = float(v)
        except ValueError:
            continue
        out.add(f"{f:g}")
    return out


class EvidenceStore:
    def __init__(self, db: Database, max_excerpt_chars: int = 300):
        self.db = db
        self.max_excerpt_chars = max_excerpt_chars

    # -- evidence ---------------------------------------------------------------------------
    def record(
        self,
        task_id: str,
        *,
        kind: str,
        source_url: str,
        captured_at: str,
        content_for_hash: str,
        final_url: str | None = None,
        source_date: str | None = None,
        excerpt: str | None = None,
        locator: dict[str, Any] | None = None,
        image_artifact_id: str | None = None,
        region: Rect | None = None,
        video_interval: VideoInterval | None = None,
        limitations: str | None = None,
        provider: str | None = None,
        stable_key: str | None = None,
    ) -> EvidenceRecord:
        # a stable key makes re-recording after a replayed step idempotent
        eid = f"evd_{sha256_text(f'{task_id}:{stable_key}')[:26].upper()}" if stable_key else new_id("evd")
        if excerpt:
            excerpt = truncate(excerpt.strip(), self.max_excerpt_chars)
        rec = EvidenceRecord(
            evidence_id=eid, task_id=task_id, kind=kind, source_url=source_url, final_url=final_url,  # type: ignore[arg-type]
            source_date=source_date, captured_at=captured_at, excerpt=excerpt, locator=locator or {},
            image_artifact_id=image_artifact_id, region=region, video_interval=video_interval,
            content_sha256=sha256_text(content_for_hash), limitations=limitations, provider=provider,
        )
        self.db.execute(
            """INSERT OR REPLACE INTO evidence(id, task_id, kind, source_url, final_url, source_date, captured_at, excerpt,
               locator_json, image_artifact_id, region_json, video_interval_json, content_sha256, limitations, provider,
               created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, task_id, kind, source_url, final_url, source_date, captured_at, excerpt, dumps(rec.locator),
             image_artifact_id, dumps(region.model_dump()) if region else None,
             video_interval.model_dump_json() if video_interval else None, rec.content_sha256, limitations, provider,
             now_iso()),
        )
        return rec

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        r = self.db.one("SELECT * FROM evidence WHERE id=?", (evidence_id,))
        return self._row(r) if r else None

    def for_task(self, task_id: str) -> list[EvidenceRecord]:
        return [self._row(r) for r in self.db.query("SELECT * FROM evidence WHERE task_id=? ORDER BY created_at", (task_id,))]

    def _row(self, r: Any) -> EvidenceRecord:
        claim_ids = [x["claim_id"] for x in self.db.query("SELECT claim_id FROM claim_evidence WHERE evidence_id=?", (r["id"],))]
        return EvidenceRecord(
            evidence_id=r["id"], task_id=r["task_id"], kind=r["kind"], source_url=r["source_url"],
            final_url=r["final_url"], source_date=r["source_date"], captured_at=r["captured_at"], excerpt=r["excerpt"],
            locator=loads(r["locator_json"], {}), image_artifact_id=r["image_artifact_id"],
            region=Rect(**loads(r["region_json"])) if r["region_json"] else None,
            video_interval=VideoInterval.model_validate_json(r["video_interval_json"]) if r["video_interval_json"] else None,
            content_sha256=r["content_sha256"], limitations=r["limitations"], provider=r["provider"], claim_ids=claim_ids,
        )

    # -- claims -----------------------------------------------------------------------------
    def add_claim(self, task_id: str, text: str, evidence_ids: list[str], support: str = "supported",
                  note: str | None = None, stable_key: str | None = None) -> Claim:
        cid = f"clm_{sha256_text(f'{task_id}:{stable_key}')[:26].upper()}" if stable_key else new_id("clm")
        known = {e.evidence_id for e in self.for_task(task_id)}
        valid = [e for e in evidence_ids if e in known]
        if not valid:
            support = "unsupported"
            note = note or "no evidence record supports this statement"
        with self.db.tx() as c:
            c.execute("DELETE FROM claim_evidence WHERE claim_id=?", (cid,))
            c.execute("INSERT OR REPLACE INTO claims(id, task_id, text, support, review_note, created_at) VALUES (?,?,?,?,?,?)",
                      (cid, task_id, text, support, note, now_iso()))
            for e in valid:
                c.execute("INSERT OR IGNORE INTO claim_evidence(claim_id, evidence_id) VALUES (?,?)", (cid, e))
        return Claim(claim_id=cid, task_id=task_id, text=text, evidence_ids=valid, support=support, review_note=note)  # type: ignore[arg-type]

    def update_claim(self, claim_id: str, support: str, note: str | None) -> None:
        self.db.execute("UPDATE claims SET support=?, review_note=? WHERE id=?", (support, note, claim_id))

    def claims(self, task_id: str) -> list[Claim]:
        out = []
        for r in self.db.query("SELECT * FROM claims WHERE task_id=? ORDER BY created_at", (task_id,)):
            eids = [x["evidence_id"] for x in self.db.query("SELECT evidence_id FROM claim_evidence WHERE claim_id=?", (r["id"],))]
            out.append(Claim(claim_id=r["id"], task_id=task_id, text=r["text"], evidence_ids=eids, support=r["support"],
                             review_note=r["review_note"]))
        return out


class ObservationStore:
    """Temporary observations: kept apart from stable memory and expired by retention."""

    def __init__(self, db: Database):
        self.db = db

    def save(self, obs: Observation) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO observations(id, task_id, worker_id, session_id, url, title, captured_at, viewport_json,
               screenshot_artifact_id, screenshot_sha256, elements_json, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (obs.observation_id, obs.task_id, obs.worker_id, obs.session_id, obs.url, obs.title, obs.captured_at,
             obs.viewport.model_dump_json(), obs.screenshot_artifact_id, obs.screenshot_sha256,
             dumps([e.model_dump() for e in obs.elements]), obs.model_dump_json()),
        )

    def get(self, observation_id: str) -> Observation | None:
        r = self.db.one("SELECT data_json FROM observations WHERE id=?", (observation_id,))
        return Observation.model_validate_json(r["data_json"]) if r else None

    def latest(self, task_id: str) -> Observation | None:
        r = self.db.one("SELECT data_json FROM observations WHERE task_id=? ORDER BY captured_at DESC LIMIT 1", (task_id,))
        return Observation.model_validate_json(r["data_json"]) if r else None

    def purge_before(self, cutoff: str) -> int:
        cur = self.db.execute("DELETE FROM observations WHERE captured_at < ?", (cutoff,))
        return cur.rowcount
