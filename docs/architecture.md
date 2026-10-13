# Architecture

This maps the specification's logical architecture (§5, figure 1) onto the code. One orchestrator
exists: the **coordinator**. OpenClaw is the owner-facing gateway; it is not a second task database and
its tools are restricted to the coordinator's MCP tools.

## Components and where they live

| Spec component (§6) | Responsibility | Code | Persistent output |
|---|---|---|---|
| OpenClaw gateway | Telegram connection, owner routing, conversation | `integrations/openclaw/omca-plugin/`, `src/omca/integrations/openclaw.py` (config translation) | Framework session state |
| Coordinator extension | Task decomposition, durable workflow state, verification | `coordinator.py`, `engine.py`, `tasks.py`, `workflows/` | `tasks`, `task_steps`, `actions`, `task_events` |
| Policy service | Classify operations, bind approvals, deny ambiguous consequential actions, refuse private network destinations | `policy/operations.py`, `policy/gate.py`, `policy/approvals.py`, `netguard.py` | `policy_versions`, `approvals`, `standing_rule_uses` |
| Model adapter | Open-model registry, modality checks, prompt assembly, provider compatibility, usage | `models/registry.py`, `models/router.py`, `models/providers.py`, `models/probes.py`, `models/gateway.py`, `models/endpoints.py` | `model_calls`, `route_probes`, `endpoint_sessions` |
| Budget | Atomic reservations, settlement, thresholds | `budget.py` | `model_calls`, `budget_notices` |
| Worker manager | Registration, capabilities, leases, dispatch, health, cancellation | `workers.py` | `workers`, `leases`, `resources`, `jobs` |
| Windows worker | Browser/app observation and execution in its own interactive session | `worker/agent.py`, `worker/browser.py`, `worker/media.py`, `worker/office.py` | Receipt cache, uploaded artifacts |
| Search adapter | Normalise search and retrieval | `research/search.py` | Search/retrieval results in action receipts |
| Evidence service | Link claims to captures, images and video intervals | `evidence.py` | `evidence`, `claims`, `claim_evidence`, `observations` |
| Report helper | Produce, reopen, validate and version documents | `reports.py`, `workflows/common.py::finalize_report` | `artifacts` (with validation results) |
| Scheduler | Durable schedules, dedup, overlap, missed runs | `scheduler.py` | `schedules`, `schedule_runs` |
| Channel | Owner commands, notifications, approvals | `channels/` | `outbox`, `inbound_updates` |
| Stable memory (FR13) | Preferences, procedures, freshness rules | `memory.py` | `preferences`, `procedures`, `freshness_rules` |

## Process layout (§7)

* **Coordinator** (`omca run`): FastAPI server (`api.py`), task engine loop, outbox sender, scheduler,
  maintenance (approval expiry, worker health, budget notices, endpoint windows, retention). May run as a
  service. In direct Telegram mode it also long-polls the Bot API.
* **Worker** (`omca worker`): one per Windows VM, started *in the interactive desktop session* (Task
  Scheduler "at log on"), never as a service. It long-polls the coordinator for fenced jobs over HTTPS with
  a per-worker credential. `omca run --with-worker vm1` runs the worker in-process for the single-VM P1
  setup (the coordinator process must then itself run in the interactive session).
* **Model server**: one local Ollama (or other OpenAI-compatible) service on a suitable host, reached over
  an authenticated private route; hosted routes are alternatives. Workers never load models.
* **OpenClaw gateway** with the OMCA plugin and the stdio MCP server (`omca mcp`).

## The governed action path (§8, §14, §15)

Every external effect goes through `TaskContext.act()`:

1. **Persist intent** (`actions` row, status `INTENDED`, idempotency key `task:step`).
2. **Policy gate**: allowlisted operation → restricted argument schema → freshness of the observation →
   deterministic classification (`READ`, `NAVIGATION`, `DRAFT`, `CREATE_OUTPUT`, `CONSEQUENTIAL`,
   `UNKNOWN_MUTATION`, `FORBIDDEN`) → verdict `ALLOW`, `REQUIRE_APPROVAL` or `DENY`.
3. **Approval** (if required): signed binding of owner, task, action digest, destination, preconditions,
   policy version, expiry and nonce; Telegram buttons; task enters `AWAITING_APPROVAL` and releases the UI
   lease while waiting. After the decision the target is **re-observed and revalidated**; the nonce is
   consumed atomically only if nothing changed.
4. **Limits**: executed-action count and active-time budget.
5. **Lease + fencing**: the UI lease (`vm1:ui`) serialises all desktop/browser interaction on a VM; the job
   carries the lease's fencing token and the worker refuses older tokens.
6. **Persist `DISPATCHED`**, then execute (coordinator executor or worker job).
7. **Persist the receipt**; mutating browser actions return a fresh observation and an expected-result
   verification. A receipt with `UNKNOWN` status is never retried automatically for consequential
   operations: the worker's receipt cache is consulted, then the owner.

Model calls go through `TaskContext.model()` → `ModelRouter.call()`: role → allowlisted route chain →
modality and context checks → conservative reservation → provider call → usage settlement.

## Durability and replay (FR02, FR14)

Workflows are plain async functions. Each model call, observation-refresh decision, retrieval and
evidence-recording batch is a **journaled step** (`task_steps`): on restart, or on resume after a pause,
the workflow re-runs and journaled steps return their stored outputs instead of executing again. Only the
frontier executes live. Evidence and claim IDs are derived from stable step keys so a re-recorded step is
idempotent. Browser workflows re-check the live page at the first observation after any (re)start and
restore the last known page if the browser session was lost.

## General mode and sub-tasks

`workflows/general.py` runs a tool loop: the model's tool calls (web search, page reading, Word documents)
execute through `ctx.act`, and their results go back to it as `tool` messages until it replies without one.
Heavy work is delegated with `TaskContext.subtask()`, which creates a child task (`origin = "subtask"`,
`params.parent_task_id`) in the same conversation, starts it at once rather than queueing it behind its
parent, and waits until it ends, blocks or pauses. The child keeps its own journal, evidence, approvals and
delivery. Cancelling the parent cancels the child; chat history (`workflows/chat.py::_turns`) skips children.

## Task lifecycle (figure 3)

`QUEUED → RUNNING → {AWAITING_APPROVAL, AWAITING_USER, BLOCKED, PAUSED} → RUNNING → VERIFYING →
{COMPLETED, PARTIAL, FAILED}`, plus `CANCELLED` from any non-terminal state. Transitions are enforced in
`tasks.py`; `COMPLETED` is reachable only from `VERIFYING` and only when the workflow's checks pass
(e.g. report valid, saved with matching checksum, delivered, at least one supported claim).
`BLOCKED` covers budget and worker unavailability; worker-blocked tasks are retried automatically,
budget-blocked tasks wait for the owner (`/resume <id> +50`).

## Workspace layout (§16)

```
<workspace_root>/
  tasks/<task-id>/inputs/        owner attachments, downloads
  tasks/<task-id>/observations/  screenshots (7-day retention)
  tasks/<task-id>/evidence/      fetched images, crops (kept with the report)
  tasks/<task-id>/media/         frames, media, transcripts (7-day retention)
  tasks/<task-id>/reports/       generated reports (kept)
  tasks/<task-id>/outputs/       drafts, workbook copies (kept)
  reports/                       owner-visible copies (configurable)
<data_dir>/omca.sqlite3          single-writer SQLite (WAL, synchronous=FULL)
<data_dir>/secrets/              generated approval signing key (if no keyring ref is set)
```

Files are written atomically, checksummed and type-sniffed independently of their names; paths are
sanitised and confined to the workspace. Workers never touch the database: they return receipts and
upload declared outputs through the coordinator.

## Interfaces (§19)

`contracts/openapi.yaml` (generated) documents the proposed controller and worker API:
`POST /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel`, `POST /approvals/{id}/decisions`,
`POST /workers/register`, `POST /workers/{id}/jobs` (fenced job dispatch to the worker, long poll),
`POST /workers/{id}/receipts`, `GET /budget`, plus pause/resume/steer/answer, artifact upload/download,
the OpenClaw bridge (`/bridge/openclaw/*`) and the metering gateway (`/v1/chat/completions`).
`contracts/schemas.json` holds the contract schemas **and** the restricted argument schema of every
allowlisted operation. A test fails if either file drifts from the code.
