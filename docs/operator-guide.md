# Operator guide (Windows)

This walks from an empty Windows VM to the P1 release gate, then P2 and P3. Running on a Mac instead? Follow
[macos.md](macos.md) for installation, secrets, autostart and permissions; the remaining sections here apply
unchanged. Commands are PowerShell
unless noted. Decisions the spec leaves to kickoff (§23) are called out as **Decide**.

## 0. Kickoff decisions

| Decision | Default in this repo | Action |
|---|---|---|
| Hardware | One Windows VM for coordinator + worker; model server on the host or a GPU box | **Decide** after checking RAM, GPU/VRAM, disk and virtualization support |
| Browser | Dedicated Microsoft Edge profile on the VM (`persistent_profile`) | **Decide** VM browser vs. attaching to the owner's physical browser (`cdp`, separate lease) |
| Local model | `qwen3.5:9b` (quantised) via Ollama, bounded context | Probe, then benchmark against the hosted route |
| Hosted model | `Qwen/Qwen3.5-35B-A3B` on DeepInfra, disabled until priced | Probe before enabling |
| Search | Firecrawl API (separate spending limit) | Supply the key; SearXNG is the drop-in alternative |
| Framework | `openclaw@2026.10.2` | Pin the version that passes the integration and enforcement probes |
| Reports | Word (`.docx`) mandatory, PDF companion off | Choose the owner-visible reports folder |
| P3 app | Spreadsheet update + email draft/send through bounded adapters | Email/calendar accounts stay unselected until needed |
| Retention | Screenshots/frames 7 days, action logs 90 days, ledger 12 months, reports kept | Review |

## 1. Install the application

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id astral-sh.uv -e
winget install --id Gyan.FFmpeg -e            # ffmpeg + ffprobe on PATH (video, P2)
git clone https://github.com/madhuakrmitha-glitch/Autonomus-Bot C:\OMCA\app
cd C:\OMCA\app
uv sync --extra all                           # exact versions from uv.lock
uv run playwright install chromium            # not needed if you only use the msedge channel
uv run omca init                              # creates config\application.yaml and config\worker.yaml
uv run pytest -q                              # optional: the deterministic suite (needs ffmpeg)
```

## 2. Secrets (references only)

Configuration never contains a literal secret: values are `env:NAME`, `keyring:SERVICE/USER` (Windows
Credential Manager) or `file:PATH`. Secrets used **only by OMCA** live in the keyring; secrets **shared with
OpenClaw** must be readable by both, so keep them as user environment variables and reference them with
`env:` in `application.yaml`.

```powershell
# OMCA-only secrets
uv run omca secrets set keyring:omca/deepinfra-api-key
uv run omca secrets set keyring:omca/firecrawl-api-key
uv run omca secrets set keyring:omca/worker-vm1 --generate --show      # also put this into worker.yaml's ref

# Shared with OpenClaw (user environment)
$rand = { -join ((1..48) | % { [char[]]'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789' | Get-Random }) }
[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "<token from @BotFather>", "User")
[Environment]::SetEnvironmentVariable("OMCA_OWNER_TOKEN", (& $rand), "User")
[Environment]::SetEnvironmentVariable("OMCA_BRIDGE_TOKEN", (& $rand), "User")
[Environment]::SetEnvironmentVariable("OMCA_MODEL_GATEWAY_TOKEN", (& $rand), "User")
[Environment]::SetEnvironmentVariable("OPENCLAW_GATEWAY_TOKEN", (& $rand), "User")
```

Then in `config\application.yaml`:

```yaml
telegram:
  bot_token_ref: env:TELEGRAM_BOT_TOKEN
  openclaw: {bridge_token_ref: env:OMCA_BRIDGE_TOKEN}
server:
  owner_api_token_ref: env:OMCA_OWNER_TOKEN
  model_gateway_token_ref: env:OMCA_MODEL_GATEWAY_TOKEN
```

The approval signing key is generated under `data\secrets\` on first run unless
`server.approval_signing_key_ref` is set. Rotating it invalidates pending approvals (by design).

## 3. Telegram owner

1. Create a bot with **@BotFather**, keep its token in `TELEGRAM_BOT_TOKEN`.
2. Put the owner's numeric user id in `owner.telegram_user_ids`. If you do not know it, use direct mode once:
   `telegram.mode: direct`, start the coordinator, run `uv run omca telegram pair`, send `/pair <code>` from
   the owner's account within 10 minutes, then copy the paired id into the config.
3. Release check (WP01): message the bot from a *different* account - nothing happens and the attempt is
   audited (`task_events.kind = unauthorized_message`).

## 4. Open models

**Local (recommended first):** install Ollama on the model host, `ollama pull qwen3.5:9b`, and make it
reachable from the VM over a private, authenticated route (bind to localhost on the same host, or put a
token-checking reverse proxy in front - Ollama itself has no authentication). Record the exact tag/digest
in the route's `revision`. Then:

```powershell
# set models.routes[local-qwen35-9b].enabled: true and its base_url
uv run omca models probe local-qwen35-9b
uv run omca models list
```

A route serves roles only after all probes pass for its current configuration digest: text, screenshot
interpretation, image + tool declaration in one request, structured tool call, tool-result continuation,
bounded output, usage reporting (or conservative estimates), and rejection of a disabled route. Changing
the model, URL, revision or modalities invalidates the probes.

**Hosted:** fill `pricing` (input/output per million tokens, `source`, `effective_date`) and
`budget.fx.USD` (rate, source, date), enable the route and probe it. Without prices and FX the route stays
disabled - placeholders never activate it.

**Dedicated endpoint (time-billed):** configure `dedicated` (namespace, name, token), then
`uv run omca endpoint start hf-dedicated-qwen35 --hours 1`. The window (plus initialization time) is
reserved against the budget first; at the end of the window the coordinator pauses the endpoint and settles
the running time.

## 5. Browser session

1. Create the dedicated profile and log in to the sites the assistant may use, by hand:
   `& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --user-data-dir=C:\OMCA\profiles\vm1-edge`
2. Close Edge, then set `authorized: true` for `vm1-edge` in **both** `application.yaml` and `worker.yaml`.
   Existing login state is not authorization to send or change anything: those still need approval.
3. To attach to an owner-started browser instead, start it with a dedicated profile and
   `--remote-debugging-port=9222` (loopback only; recent Chrome/Edge refuse remote debugging on the default
   profile) and use `mode: cdp`. Never expose the debugging port beyond localhost.

## 6. OpenClaw

Install the pinned version (Node 24.16+ or 26 required):

```powershell
iwr -useb https://openclaw.ai/install.ps1 | iex      # or: npm install -g openclaw@2026.10.2
openclaw onboard --non-interactive --accept-risk --skip-health
uv run omca openclaw config > $env:USERPROFILE\.openclaw\openclaw.generated.json5
```

Review the generated file and merge it into `%USERPROFILE%\.openclaw\openclaw.json` (keep `gateway.mode:
"local"`). It configures: Telegram with `dmPolicy: "allowlist"`, the coordinator as the only model
provider (`models.mode: "replace"`, no fallbacks), the `omca` MCP server, `tools.allow: ["omca__*"]`, and
the OMCA plugin. Then:

```powershell
openclaw plugins install --link C:\OMCA\app\integrations\openclaw\omca-plugin
openclaw plugins enable omca
openclaw config validate
openclaw gateway install            # Scheduled Task; or `openclaw gateway` in a terminal
openclaw mcp doctor omca --probe
openclaw plugins inspect omca --runtime --json
```

Details and the image-path spike: [openclaw.md](openclaw.md). If OpenClaw is not wanted, set
`telegram.mode: direct` and the coordinator polls Telegram itself.

## 7. Run the coordinator and worker

Browser and desktop automation need the **interactive** desktop, so OMCA starts at log on of the VM user,
not as a service:

```powershell
.\scripts\windows\register-tasks.ps1 -AppDir C:\OMCA\app -Mode all-in-one -WorkerId vm1
Start-ScheduledTask -TaskPath '\OMCA\' -TaskName Coordinator
```

(Equivalent by hand: Task Scheduler task, trigger *At log on*, *Run only when user is logged on*, action
`uv.exe run omca run --with-worker vm1` started in `C:\OMCA\app`.)

Keep the VM powered and awake (power plan: never sleep). Disconnecting or locking an RDP session can break
input automation (S06): use the hypervisor console/VNC, or return a disconnected RDP session to the
console with `tscon <id> /dest:console` instead of closing the window.

## 8. P0/P1 release checks

| Check | How |
|---|---|
| Probes pass, closed routes blocked | `omca models probe <route>`; `omca models list` shows any closed/unpriced route as unusable |
| Only the owner reaches the assistant | Message from another account → no reply, audited |
| Deterministic suite three times | `uv run omca fixtures --repeat 3` → `data\fixture-results\<stamp>\summary.json` (the development record is in `docs/release/`; keep yours alongside it) |
| Live smoke | One real research task per selected search provider; compare report claims against sources |
| Recovery demonstration | Start a `/browse` task that needs approval, approve it, stop the coordinator mid-task, restart: the action is not repeated and the task resumes or asks you |

### Demonstration tasks and expected artifacts

| Telegram message | Expected result |
|---|---|
| `/research rooftop solar adoption in India` | Ack within seconds; progress; a `.docx` in Telegram (caption shows the SHA-256 prefix) and in the reports folder; task `COMPLETED`; report sections with `[E1]` citations, a Sources list with retrieval times, and a Limitations section |
| `/research <topic> including charts` | As above plus a Figures section; chart values only if confirmed on a zoomed re-read, otherwise listed under limitations |
| `/video https://<accessible video page> what are the steps?` | Timestamped report; method states captions or Whisper; coverage limitation lists the sampling |
| `/browse go to <site> and find <fact>` | Short verified summary; any submit/send/delete click shows **Approve once / Deny** first |
| `/office update totals in the attached workbook and draft an email to <contact>` (attach `.xlsx`) | New workbook copy and `.eml` preview; sending asks for approval and only goes to configured contacts |
| `/pause <id>`, `/resume <id>`, `/cancel <id>` | No new actions while paused; cancellation reports completed effects |

## 9. Schedules (P2)

Add schedules in `application.yaml` (`schedules:`) or from Telegram:
`/schedule add weekly "45 8 * * 1" Weekly rooftop solar brief as of {date}`. Each firing is deduplicated by
schedule id + planned time, overlapping runs are skipped (`concurrency: forbid`), and missed runs follow
`missed_run_policy` (`skip` or `run_latest`). `/pause all` pauses tasks and schedules.

## 10. Second VM (P3)

1. Give the coordinator a TLS certificate (`server.tls_cert_file`, `tls_key_file`, `public_base_url`) and
   open its port only on the private network.
2. On VM two: install as in §1, create `config\worker.yaml` with `worker_id: vm2`, `coordinator_url:
   https://...`, `ca_file`, its own credential and its own browser profile.
3. Add `vm2` to `workers:` on the coordinator; start `uv run omca worker` from VM two's interactive session.
4. Route explicitly with `TaskRequest.worker_id` (API/MCP) or let capability routing pick an idle worker.
   Each VM has its own UI lease; artifacts move between workers only as declared, hash-checked job inputs.

## 11. Budget operations

* `/budget` or `uv run omca budget` - committed, reserved, unknown, remaining, disabled routes and why.
* `uv run omca budget ledger` - per-call rows (route, purpose, caller, reservation, actual cost).
* Calls without reported usage keep their conservative reservation as `UNKNOWN`. Reconcile them against the
  provider invoice: `uv run omca budget reconcile <call_id> <actual_inr> --note "invoice 2026-10"`.
* Notices are sent at 70 % and 90 % of the monthly ceiling. A budget-blocked task resumes with
  `/resume <id>` (optionally `+20` to raise its per-task ceiling).

## 12. Backup and recovery

* **What to back up:** `data\omca.sqlite3` (tasks, evidence, approvals, ledger), `workspace\` (reports,
  evidence, outputs), `config\`. Stop the coordinator first, or use `sqlite3 data\omca.sqlite3 ".backup
  backup.sqlite3"` while it runs. A VM snapshot is operational recovery, not a substitute.
* **After a crash or restart:** the coordinator replays every unfinished task from its journal. Read-only
  steps are retried; a consequential action whose outcome is unknown is reconciled with the worker's receipt
  cache and, if that cannot establish the outcome, the owner is asked (`done` / `not done` / `stop`). Sends,
  purchases, invitations and deletions are never repeated blindly.
* **Lost worker:** its lease expires; jobs from its old process are fenced out (new session epoch, newer
  fencing token). Restart the worker; uncertain jobs are reconciled before further work.
* **Retention:** applied hourly; run immediately with `uv run omca retention run`.

## 13. Upgrades

Upgrade one integration at a time (OpenClaw, Playwright, a model revision, a search provider), then rerun
`uv run omca models probe <route>`, `uv run omca fixtures --repeat 3` and the live smoke task. Record
versions, model revision and license in [compatibility.md](compatibility.md).
