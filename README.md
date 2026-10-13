# Open Model Computer Assistant (OMCA)

A single-owner personal assistant that takes work through **Telegram**, researches with the owner's
browser and supplied search APIs, **sees** webpage images and video frames, writes **source-backed Word
reports**, and operates **Windows** tools through bounded adapters. All application-controlled inference
runs on **verified open-weight models** (local or approved hosted routes), inside a shared monthly budget,
and every consequential action waits for the owner's explicit, action-bound approval.

It implements *Open Model Computer Assistant Implementation Specification v1.0* (12 Oct 2026) on top of
**OpenClaw** (gateway, Telegram, conversation) with a Python **coordinator sidecar** (task state, policy,
approvals, budget, model routing, evidence) and **Windows workers** (browser, media, office).

```
Telegram owner ──► OpenClaw gateway ──► OMCA plugin + MCP tools ──► Coordinator (this repo)
                                                                     ├─ task engine (journaled, resumable)
                                                                     ├─ policy gate + approvals
                                                                     ├─ budget ledger + open-model router ──► Ollama / DeepInfra / HF
                                                                     ├─ search adapters (Firecrawl, SearXNG)
                                                                     ├─ evidence ledger + Word report helper
                                                                     └─ worker manager (leases, fencing) ──► Windows VM worker(s)
                                                                                                             ├─ Playwright browser
                                                                                                             ├─ yt-dlp / FFmpeg media
                                                                                                             └─ office adapters
```

## Status

| Phase | Deliverable | State |
|---|---|---|
| P0 | One VM, Telegram owner pairing, open-model route, secrets, shared cost accounting | Implemented; release gate needs probes against *your* model route |
| P1 | Research, browser navigation, webpage image analysis, evidence ledger, Word report, progress, approval, recovery | Implemented; AT01-AT15 pass on local fixtures |
| P2 | Website video analysis (frames + transcript), scheduled research | Implemented; AT16-AT21 pass on local fixtures |
| P3 | Second VM, explicit job routing, bounded desktop/office workflow | Implemented; AT22-AT25 pass on local fixtures |

The deterministic suite (`148` tests) runs without API keys. **Production enablement still requires** the
owner-side steps in [docs/operator-guide.md](docs/operator-guide.md): real secrets, a probed open-model
route, an authorized browser profile and a recovery demonstration. Known limitations are listed in
[docs/limitations.md](docs/limitations.md).

## Quick start (development)

```bash
uv sync --extra browser --extra video --extra mcp      # Python 3.11+; installs pinned versions from uv.lock
uv run pytest                                          # 148 deterministic tests (4 live tests skip)
uv run omca fixtures --repeat 3                        # release record: three consecutive runs + versions
                                                       # (latest record: docs/release/)
```

Playwright needs Chromium (`uv run playwright install chromium`) and the video tests need `ffmpeg` on PATH.

## Platforms

| Platform | Coordinator | Worker (browser, media, office) | Raw desktop input | Autostart in the desktop session | Guide |
|---|---|---|---|---|---|
| Windows 10/11 (reference) | yes | yes | pywinauto | `scripts/windows/register-tasks.ps1` | [operator-guide.md](docs/operator-guide.md) |
| macOS 13+ (Apple silicon or Intel) | yes | yes | pyautogui (Accessibility permission) | `omca service install` (LaunchAgent) | [macos.md](docs/macos.md) |
| Linux | yes | browser, media, office | no | systemd user unit (not provided) | development and CI |

Workers talk to the coordinator over HTTPS, so a Mac coordinator can drive Windows VM workers and vice versa.

## Running it for real (Windows)

Full, step-by-step instructions: **[docs/operator-guide.md](docs/operator-guide.md)** (macOS:
**[docs/macos.md](docs/macos.md)**). In short:

```powershell
uv sync --extra all                                    # on the Windows VM
uv run omca init                                       # creates config/application.yaml and config/worker.yaml
uv run omca secrets set keyring:omca/telegram-bot-token
uv run omca secrets set keyring:omca/owner-api-token --generate --show     # also: bridge, gateway, worker tokens
uv run omca models probe local-qwen35-9b               # a route serves no role until its probes pass
uv run omca openclaw config > %USERPROFILE%\.openclaw\openclaw.json      # framework settings, secrets as ${ENV}
uv run omca run --with-worker vm1                      # coordinator + in-process worker, in the logged-in session
```

Then message the bot: `/research rooftop solar adoption in India` → a cited `.docx` arrives in Telegram
and is saved to the reports folder.

## Local web UI

With `ui.enabled: true`, the coordinator also serves a chat UI at `http://127.0.0.1:8765/ui`: the same
commands, free-text tasks, approval buttons and report downloads as Telegram, plus a task sidebar with
pause/resume/cancel and the month's budget. `uv run omca ui` opens it signed in (the owner API token is
passed in the URL fragment and kept in the browser's local storage). Every owner message is mirrored to the
UI; when Telegram is not configured the UI is the delivery channel, and when it is, both receive messages.

## Browser engines

`/browse` runs on the built-in step loop (one model-chosen action at a time, each checked by the policy gate)
or, with `browser.engine: browser_use`, on the [Browser Use](docs/browser-use.md) agent in its own virtualenv,
which navigates real websites more reliably. Its model calls still go through the metered gateway.

## Owner commands (Telegram)

`/research <topic>` · `/video <url> [question]` · `/browse <goal>` · `/office <goal>` · `/tasks` ·
`/status <id>` · `/pause <id|all>` · `/resume <id> [+budget]` · `/cancel <id>` · `/steer <id> <text>` ·
`/answer <id> <text>` · `/budget` · `/schedules` · `/schedule add|pause|resume|run` · `/prefs`.
Approvals arrive with **Approve once / Deny** buttons. With OpenClaw, `/status`, `/pause`, `/resume`,
`/cancel`, `/approve`, `/deny` are spelled `/omca status …` because OpenClaw reserves those names.

## Repository layout

| Path | Contents |
|---|---|
| `src/omca/` | Coordinator, worker, workflows (see [docs/architecture.md](docs/architecture.md) for the module map) |
| `config/application.example.yaml` | Normative default inventory (§19); `config/worker.example.yaml` per VM |
| `contracts/schemas.json`, `contracts/openapi.yaml` | Application contracts and API, generated from the enforcing code (`omca contracts export`) |
| `integrations/openclaw/omca-plugin/` | OpenClaw plugin: fail-closed tool gate, owner commands, approval buttons |
| `src/omca/db/migrations/` | SQLite schema migrations (applied automatically) |
| `tests/` | Acceptance fixtures (local site, generated videos, scripted model), `acceptance_cases.json` |
| `docs/` | Architecture, operator guide, OpenClaw integration, policy, traceability, limitations, release results |

## Design commitments

- **Open models only.** Every role (planning, vision, summarisation, review, fallbacks, and OpenClaw's own
  calls via the metering gateway) resolves through an allowlist of open-weight routes with recorded
  licenses and passing capability probes. Closed-model names are rejected even if mis-declared.
- **No action from intention.** Success is established by a receipt and a fresh observation, never because
  the model asked for it. Consequential effects are never repeated blindly after a restart.
- **Approval is bound to the exact action** (digest, destination, preconditions, policy version, owner,
  expiry, single-use nonce, HMAC signature) and revalidated against the live target before execution.
- **Evidence first.** Quotes are verified verbatim against the captured source; numbers must appear in the
  cited evidence; unreadable chart values are confirmed on zoom or reported as uncertain; transcripts are
  never presented as visual understanding; video coverage is disclosed.
- **Budget is enforced, not estimated afterwards.** Conservative reservations are taken atomically against
  the shared INR 2,000 monthly ceiling before any paid call.
