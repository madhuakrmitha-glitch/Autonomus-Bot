# OpenClaw integration

OpenClaw remains unmodified. A plugin plus the coordinator sidecar enforce the boundaries the spec
requires (§5, §6), because OpenClaw's native command approvals do not cover browser and desktop
operations by themselves. Pinned host: **openclaw@2026.10.2** (plugin APIs are marked experimental there).

## What each piece does

| Piece | Mechanism | Purpose |
|---|---|---|
| Telegram channel | `channels.telegram` with `dmPolicy: "allowlist"`, `allowFrom: [owner id]` | Only the owner reaches the agent |
| Model provider | `models.mode: "replace"`, one provider `omca` → `http://<coordinator>/v1`, `fallbacks: []`, `utilityModel: ""` | Framework-owned planning and helper calls pass the same open-model allowlist and shared budget (§5) |
| MCP server | `mcp.servers.omca` → `omca mcp` (stdio) | Task tools only: `create_task`, `task_status`, `list_tasks`, `pause_task`, `resume_task`, `cancel_task`, `steer_task`, `budget`, `task_image` |
| Tool policy | `tools.allow: ["omca__*"]` and deny groups `runtime, fs, ui, web, nodes, automation, sessions, memory, media` | No raw exec, file, browser or network tools in the agent |
| Plugin `before_tool_call` | Blocks any tool not prefixed `omca__` and any non-owner requester; fails closed on error/timeout | Defence in depth if tool policy is misconfigured |
| Plugin commands | `/research`, `/video`, `/browse`, `/office`, `/tasks`, `/steer`, `/answer`, `/budget`, `/schedules`, `/schedule`, `/prefs`, `/omca …` | Handled without the LLM; forwarded with OpenClaw's verified `senderId` to `POST /bridge/openclaw/message` |
| Plugin interactive handler | Namespace `omca`: callback data `omca:apr:<approval id>:y|n` | Approval buttons forwarded to `POST /bridge/openclaw/callback`; the coordinator authenticates the approver against `owner.approver_user_ids` |
| Outbound delivery | Coordinator → Telegram Bot API with the same bot token | Third-party plugins cannot start sends; Bot API sends do not conflict with OpenClaw's polling |

The bridge endpoints require `OMCA_BRIDGE_TOKEN`. Neither the bridge nor any tool accepts an owner
identity from a payload: the sender id comes from OpenClaw's verified context, and the coordinator checks it
against its own allowlist again.

OpenClaw reserves `/status`, `/approve` and similar names, so in OpenClaw mode the coordinator's messages
spell these `/omca status <id>`, `/omca approve <approval id>`, etc.

## Conversation and task state

Task state is authoritative in the coordinator database. The OpenClaw conversation links to task IDs
(the MCP tools return them); it is not a second task database. Free-text requests go to the OpenClaw
agent, which calls `omca__create_task`; the coordinator acknowledges, sends progress, approvals and results
directly to the owner's DM.

## Image path spike (§6)

The first integration spike must prove that image observations survive the MCP return path and reach the
configured open vision model:

1. Configure the OpenClaw provider model with `input: ["text", "image"]` (the generated config does this
   when the planning route lists the `image` modality).
2. Run a research task that produces a figure, then ask the agent: *"show me the figure from task X"*.
   `omca__task_image` returns MCP image content. OpenClaw converts MCP image blocks into image inputs for
   image-capable models (`openai-completions` transport) - verify in the coordinator ledger that the
   following `/v1/chat/completions` call carries an image (`model_calls.est_images > 0`).
3. Confirm the model's answer describes the image correctly.

Application workflows themselves do not depend on this path: the coordinator sends pixels to the vision
route directly through its router, and every such call is metered.

## Schedules

The coordinator owns schedules (durable, deduplicated, restart-safe). OpenClaw cron is not needed. If you
prefer OpenClaw automations, have them call `POST /schedules/{id}/fire` with the planned time - the same
deduplication key applies, so duplicate triggers never create a second run.

## Verification commands

```powershell
openclaw config validate
openclaw doctor
openclaw plugins inspect omca --runtime --json
openclaw mcp doctor omca --probe
openclaw sandbox explain --json         # effective tool policy: only omca__* should be callable
```

## Plugin development

`integrations/openclaw/omca-plugin/index.ts` is loaded as TypeScript by OpenClaw (`--link` install, no
build step). It type-checks against the SDK shipped in `openclaw@2026.10.2`:

```bash
cd integrations/openclaw/omca-plugin
npm install --ignore-scripts openclaw@2026.10.2 typescript@5.9.3 @types/node@22
npx tsc -p tsconfig.json
```
