"""Repeat the acceptance fixture suite and keep the evidence (NFR05, spec section 21).

Each repetition runs pytest on the deterministic suite with a JUnit report. The summary
records per-case outcomes across repetitions, the git revision, runtime and component
versions and a hash of each report, so a release can show three consecutive passes.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


def _versions() -> dict[str, str]:
    out = {"python": platform.python_version(), "platform": platform.platform()}
    try:
        from importlib.metadata import version

        for pkg in ("omca", "playwright", "python-docx", "httpx", "yt-dlp", "faster-whisper", "mcp", "fastapi"):
            try:
                out[pkg] = version(pkg)
            except Exception:  # noqa: BLE001
                pass
    except ImportError:  # pragma: no cover
        pass
    ff = shutil.which("ffmpeg")
    if ff:
        res = subprocess.run([ff, "-version"], capture_output=True, text=True, check=False)
        out["ffmpeg"] = (res.stdout.splitlines() or ["?"])[0]
    try:
        out["git"] = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        pass
    return out


def _cases(xml_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    root = ET.parse(xml_path).getroot()
    for tc in root.iter("testcase"):
        name = f"{tc.get('classname')}::{tc.get('name')}"
        status = "passed"
        if tc.find("failure") is not None or tc.find("error") is not None:
            status = "failed"
        elif tc.find("skipped") is not None:
            status = "skipped"
        out[name] = status
    return out


def run_fixture_suite(repeat: int = 3, out_dir: Path = Path("data/fixture-results"), extra: list[str] | None = None) -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / stamp
    target.mkdir(parents=True, exist_ok=True)
    runs = []
    for i in range(1, repeat + 1):
        xml = target / f"run-{i}.xml"
        log = target / f"run-{i}.log"
        cmd = [sys.executable, "-m", "pytest", "-m", "not live", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}",
               *(extra or [])]
        with open(log, "w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, check=False).returncode
        cases = _cases(xml) if xml.exists() else {}
        runs.append({"run": i, "exit_code": rc, "passed": sum(v == "passed" for v in cases.values()),
                     "failed": sum(v == "failed" for v in cases.values()),
                     "skipped": sum(v == "skipped" for v in cases.values()),
                     "report_sha256": hashlib.sha256(xml.read_bytes()).hexdigest() if xml.exists() else None,
                     "cases": cases})
        print(f"run {i}/{repeat}: exit {rc}, {runs[-1]['passed']} passed, {runs[-1]['failed']} failed, "
              f"{runs[-1]['skipped']} skipped")
    all_cases = sorted({c for r in runs for c in r["cases"]})
    flaky = [c for c in all_cases if len({r["cases"].get(c) for r in runs}) > 1]
    ok = all(r["exit_code"] == 0 for r in runs)
    summary = {"started": stamp, "repeat": repeat, "all_passed": ok, "flaky_cases": flaky, "versions": _versions(),
               "runs": [{k: v for k, v in r.items() if k != "cases"} for r in runs],
               "cases": {c: [r["cases"].get(c, "missing") for r in runs] for c in all_cases}}
    (target / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"summary: {target / 'summary.json'} - {'ALL PASSED' if ok else 'FAILURES'}"
          + (f"; inconsistent cases: {len(flaky)}" if flaky else ""))
    return 0 if ok else 1
