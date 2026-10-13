/**
 * OMCA bridge plugin for OpenClaw (pinned host: openclaw@2026.10.2; plugin APIs are experimental).
 *
 * Responsibilities, all fail-closed:
 *  1. Tool boundary: `before_tool_call` blocks every tool except the coordinator's MCP tools
 *     (`omca__*`) and blocks non-owner requesters. Raw exec/browser/file tools never run.
 *  2. Owner commands (/general, /research, /video, /browse, /office, /tasks, /steer, /answer, /budget,
 *     /schedules, /schedule, /prefs and the /omca catch-all) bypass the LLM and are forwarded
 *     with OpenClaw's *verified* sender id to POST /bridge/openclaw/message.
 *  3. Approval buttons (callback data `omca:apr:<id>:y|n`) are routed to this plugin by
 *     namespace and forwarded to POST /bridge/openclaw/callback; the coordinator checks the
 *     sender against its approver allowlist and the approval binding.
 *
 * Outbound messages are sent by the coordinator itself (third-party plugins cannot start sends).
 */
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

type BridgeConfig = {
  sidecarUrl: string;
  bridgeToken: string;
  toolPrefix: string;
  allowTools: string[];
  timeoutMs: number;
};

const COMMANDS: Array<{ name: string; description: string }> = [
  { name: "general", description: "OMCA: chat that can search, read pages, write documents, research and browse" },
  { name: "research", description: "OMCA: research a topic into a cited Word report" },
  { name: "video", description: "OMCA: timestamped analysis of a website video" },
  { name: "browse", description: "OMCA: operate the authorized browser for a goal" },
  { name: "office", description: "OMCA: spreadsheet, email-draft and calendar adapters" },
  { name: "tasks", description: "OMCA: list recent tasks" },
  { name: "steer", description: "OMCA: add an instruction to a task (/steer <id> <text>)" },
  { name: "answer", description: "OMCA: answer a task's question (/answer <id> <text>)" },
  { name: "budget", description: "OMCA: model budget this month" },
  { name: "schedules", description: "OMCA: list schedules" },
  { name: "schedule", description: "OMCA: add, pause, resume or run a schedule" },
  { name: "prefs", description: "OMCA: show or change stable preferences" },
  { name: "omca", description: "OMCA: status|pause|resume|cancel|approve|deny <id>" },
];

function loadConfig(raw: Record<string, unknown> | undefined): BridgeConfig {
  const r = raw ?? {};
  return {
    sidecarUrl: String(r.sidecarUrl ?? "http://127.0.0.1:8765").replace(/\/+$/, ""),
    bridgeToken: String(r.bridgeToken ?? process.env.OMCA_BRIDGE_TOKEN ?? ""),
    toolPrefix: String(r.toolPrefix ?? "omca__"),
    allowTools: Array.isArray(r.allowTools) ? r.allowTools.map(String) : [],
    timeoutMs: Number(r.timeoutMs ?? 15000),
  };
}

async function post(cfg: BridgeConfig, path: string, body: unknown): Promise<{ reply?: string | null }> {
  if (!cfg.bridgeToken) throw new Error("OMCA bridge token is not configured");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.timeoutMs);
  try {
    const res = await fetch(`${cfg.sidecarUrl}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${cfg.bridgeToken}` },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`coordinator answered HTTP ${res.status}`);
    return (await res.json()) as { reply?: string | null };
  } finally {
    clearTimeout(timer);
  }
}

export default definePluginEntry({
  id: "omca",
  name: "OMCA coordinator bridge",
  description: "Bridges owner commands and approvals to the OMCA coordinator; restricts tools to omca__*",
  register(api) {
    const cfg = loadConfig(api.pluginConfig);

    // 1. Tool boundary (fails closed: a throw or timeout also blocks the call).
    api.on(
      "before_tool_call",
      (event, ctx) => {
        const name = String(event.toolName ?? "");
        if (!(name.startsWith(cfg.toolPrefix) || cfg.allowTools.includes(name))) {
          return { block: true, blockReason: `tool ${name} is outside the OMCA policy boundary` };
        }
        const requester = (ctx as { requester?: { senderIsOwner?: boolean } } | undefined)?.requester;
        if (requester && requester.senderIsOwner === false) {
          return { block: true, blockReason: "only the owner may use OMCA tools" };
        }
        return undefined;
      },
      { priority: 1000 },
    );

    // 2. Owner commands without the LLM.
    for (const command of COMMANDS) {
      api.registerCommand({
        name: command.name,
        description: command.description,
        acceptsArgs: true,
        requireAuth: true,
        exposeSenderIsOwner: true,
        channels: ["telegram"],
        handler: async (ctx) => {
          if (!ctx.isAuthorizedSender || ctx.senderIsOwner === false || !ctx.senderId) {
            return { text: "Not authorized." };
          }
          const text = `/${command.name} ${ctx.args ?? ""}`.trim();
          try {
            const res = await post(cfg, "/bridge/openclaw/message", { sender_id: String(ctx.senderId), text });
            return { text: res.reply ?? "OK - updates will follow here." };
          } catch (err) {
            return { text: `The OMCA coordinator is unreachable: ${(err as Error).message}` };
          }
        },
      });
    }

    // 3. Approval buttons: callback_data "omca:<payload>" is routed here by namespace.
    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "omca",
      handler: async (ctx: any) => {
        if (!ctx?.auth?.isAuthorizedSender || !ctx.senderId) return { handled: true };
        const data = `omca:${ctx.callback.payload}`;
        try {
          const res = await post(cfg, "/bridge/openclaw/callback", {
            sender_id: String(ctx.senderId),
            data,
            callback_id: `${ctx.callback.chatId}:${ctx.callback.messageId}:${ctx.callbackId ?? ""}`,
          });
          await ctx.respond.clearButtons();
          if (res.reply) await ctx.respond.reply({ text: res.reply });
        } catch (err) {
          await ctx.respond.reply({ text: `Decision not delivered: ${(err as Error).message}` });
        }
        return { handled: true };
      },
    });
  },
});
