# Release record: deterministic fixture suite

Evidence for NFR05 (three consecutive passes) and NFR06 (approval, cancellation, model restriction and
budget tests pass). Produced with `uv run omca fixtures --repeat 3`; the full per-case table is in
[`fixture-summary.json`](fixture-summary.json).

| Item | Value |
|---|---|
| Commit tested | `858ac240b2ad82895b46c2fcc3e71912f2654e50` |
| Started | 2026-10-12 14:53 UTC |
| Runs | 3 of 3 passed (exit code 0) |
| Cases per run | 148 passed, 0 failed (the 4 `live` tests are excluded: they need real routes) |
| Inconsistent cases across runs | none |
| Environment | Linux x86_64, Python 3.13.16, Playwright 1.56.0 (Chromium 1194, headless), FFmpeg 6.1.1 |

What this record covers: AT01-AT25 on local fixtures (a deterministic website, generated videos, a
scripted model that works from real pixels, a local SMTP sink), the unit and API checks, and the
regression tests for the pre-handoff safety review (`tests/test_review_regressions.py`).

What it does not cover: real open-model quality, the live Telegram bot, OpenClaw at runtime, real
Windows or macOS desktops and real accounts. Those are owner-side validation steps listed in
[`docs/operator-guide.md`](../operator-guide.md) and [`docs/limitations.md`](../limitations.md). Re-run
the suite on the target machine and replace this record after any change.
