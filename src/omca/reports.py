"""Word report helper (FR10; python-docx, reference S22).

``build_docx`` renders claims with evidence citations; ``validate_docx`` reopens the saved
bytes and checks that every claim is present, every citation marker resolves to a listed
source, every listed source is a real evidence record, and the checksum matches. A narrow,
registered helper: it never receives shell access.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from .contracts import Claim, EvidenceRecord
from .util import display_time, sha256_bytes, truncate

LABEL = re.compile(r"\[(E\d+)\]")


@dataclass
class ReportSection:
    heading: str
    claims: list[Claim] = field(default_factory=list)
    figures: list[tuple[bytes, str, list[str]]] = field(default_factory=list)  # (png bytes, caption, evidence ids)


@dataclass
class ReportInput:
    title: str
    task_id: str
    goal: str
    summary_claims: list[Claim]
    sections: list[ReportSection]
    evidence: list[EvidenceRecord]
    limitations: list[str]
    disagreements: list[str]
    method: dict[str, Any]
    timezone: str
    generated_at: str


def label_map(inp: ReportInput) -> dict[str, str]:
    order: list[str] = []
    for c in [*inp.summary_claims, *(c for s in inp.sections for c in s.claims)]:
        for e in c.evidence_ids:
            if e not in order:
                order.append(e)
    for s in inp.sections:
        for _, _, eids in s.figures:
            for e in eids:
                if e not in order:
                    order.append(e)
    return {eid: f"E{i + 1}" for i, eid in enumerate(order)}


def _claim_paragraph(doc: Any, claim: Claim, labels: dict[str, str], style: str | None = None) -> None:
    p = doc.add_paragraph(style=style)
    p.add_run(claim.text.strip())
    cites = "".join(f"[{labels[e]}]" for e in claim.evidence_ids if e in labels)
    if claim.support == "supported" and cites:
        p.add_run(" " + cites)
    else:
        marker = {"unsupported": "unsupported - no cited evidence", "disputed": "disputed",
                  "uncertain": "uncertain"}.get(claim.support, claim.support)
        if cites:
            p.add_run(" " + cites)
        r = p.add_run(f" ({marker}{': ' + claim.review_note if claim.review_note else ''})")
        r.italic = True
        r.font.color.rgb = RGBColor(0x9A, 0x34, 0x12)


def build_docx(inp: ReportInput) -> tuple[bytes, dict[str, str]]:
    labels = label_map(inp)
    doc = Document()
    styles = doc.styles
    styles["Normal"].font.name = "Calibri"
    styles["Normal"].font.size = Pt(11)
    doc.add_heading(inp.title, level=0)
    meta = doc.add_paragraph()
    meta.add_run(f"Task {inp.task_id} | generated {display_time(inp.generated_at, inp.timezone)} | "
                 f"{len(labels)} cited source records").italic = True
    doc.add_paragraph(f"Request: {truncate(inp.goal, 600)}")

    doc.add_heading("Summary", level=1)
    if inp.summary_claims:
        for c in inp.summary_claims:
            _claim_paragraph(doc, c, labels)
    else:
        doc.add_paragraph("No supported summary could be produced from the available evidence.")

    for section in inp.sections:
        doc.add_heading(section.heading, level=1)
        for c in section.claims:
            _claim_paragraph(doc, c, labels, style="List Bullet")
        for png, caption, eids in section.figures:
            doc.add_picture(io.BytesIO(png), width=Inches(5.5))
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap.add_run(caption + " " + "".join(f"[{labels[e]}]" for e in eids if e in labels)).italic = True

    if inp.disagreements:
        doc.add_heading("Where sources disagree", level=1)
        for d in inp.disagreements:
            doc.add_paragraph(d, style="List Bullet")

    doc.add_heading("Limitations and evidence gaps", level=1)
    for lim in inp.limitations or ["None recorded."]:
        doc.add_paragraph(lim, style="List Bullet")

    doc.add_heading("Sources", level=1)
    by_id = {e.evidence_id: e for e in inp.evidence}
    for eid, label in labels.items():
        e = by_id.get(eid)
        if e is None:
            continue
        p = doc.add_paragraph()
        p.add_run(f"[{label}] ").bold = True
        p.add_run(e.source_url)
        details = [f"retrieved {display_time(e.captured_at, inp.timezone)}",
                   f"published {e.source_date}" if e.source_date else "publication date not stated"]
        if e.kind == "video_interval" and e.video_interval:
            details.append(f"video {e.video_interval.start_seconds:.1f}s-{e.video_interval.end_seconds:.1f}s")
        if e.kind == "image_region":
            details.append("image region" + (f" {int(e.region.width)}x{int(e.region.height)}px" if e.region else ""))
        details.append(f"evidence {e.evidence_id}")
        p.add_run(" - " + "; ".join(details))
        if e.excerpt:
            q = doc.add_paragraph()
            q.paragraph_format.left_indent = Inches(0.4)
            q.add_run(f"“{truncate(e.excerpt, 300)}”").italic = True
        if e.limitations:
            lp = doc.add_paragraph()
            lp.paragraph_format.left_indent = Inches(0.4)
            lp.add_run(f"Limitation: {e.limitations}").italic = True

    doc.add_heading("Method", level=1)
    for k, v in inp.method.items():
        doc.add_paragraph(f"{k}: {v}", style="List Bullet")
    props = doc.core_properties
    props.title = inp.title[:200]
    props.subject = f"OMCA task {inp.task_id}"
    props.keywords = "omca;report"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), labels


def docx_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def validate_docx(data: bytes, *, expected_sha256: str | None, claims: list[Claim], labels: dict[str, str],
                  evidence_ids: set[str]) -> dict[str, Any]:
    problems: list[str] = []
    try:
        text = docx_text(data)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "problems": [f"report does not reopen: {exc}"]}
    sha = sha256_bytes(data)
    if expected_sha256 and sha != expected_sha256:
        problems.append("checksum mismatch after save")
    body, _, sources = text.partition("\nSources\n")
    for c in claims:
        if c.text.strip() not in text:
            problems.append(f"claim missing from report: {truncate(c.text, 80)}")
        for e in c.evidence_ids:
            if e in labels and f"[{labels[e]}]" not in body:
                problems.append(f"citation {labels[e]} missing for claim {truncate(c.text, 60)}")
    used = set(LABEL.findall(body))
    listed = set(re.findall(r"^\[(E\d+)\] ", sources, re.M))
    for lab in used - listed:
        problems.append(f"citation [{lab}] has no source entry")
    reverse = {v: k for k, v in labels.items()}
    for lab in listed:
        eid = reverse.get(lab)
        if eid is None or eid not in evidence_ids:
            problems.append(f"source entry [{lab}] is not a recorded evidence item")
    return {"ok": not problems, "problems": problems, "sha256": sha, "bytes": len(data), "citations_used": len(used),
            "sources_listed": len(listed), "claims_checked": len(claims)}
