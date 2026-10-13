"""Owner interaction (spec section 17): commands, free-text goals, steering, answers, approvals.

Shared by the direct Telegram poller and the OpenClaw bridge. The identity passed in comes
from the channel layer (Telegram ``from.id`` or OpenClaw's verified sender), never from a
tool payload. Anyone not on the owner allowlist is ignored and audited.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..config import ScheduleConfig
from ..contracts import TaskRequest, TaskStatus
from ..db import loads
from ..policy import Identity
from ..tasks import TransitionError
from ..util import display_time, iso, now_iso, parse_iso, random_token, truncate, utcnow

if TYPE_CHECKING:
    from ..coordinator import Coordinator

HELP = """Commands
/general <message> - chat that searches, reads pages, writes documents, researches or browses when needed
/research <topic> - cited Word report
/video <url> [question] - timestamped video analysis
/browse <goal> - operate the authorized browser
/office <goal> - spreadsheet / email / calendar adapters
/status [id], /tasks - progress
/pause <id|all>, /resume <id> [+budget], /cancel <id>
/steer <id> <instruction> (or reply to a task message)
/answer <id> <text>
/approve <approval id>, /deny <approval id>
/budget - model spend this month
/schedules, /schedule add|pause|resume|run ...
/prefs [set <key> <value> | del <key>]
Any other message goes to General, unless it looks like a video, office or browser task."""

URL = re.compile(r"https?://\S+")


class OwnerInterface:
    def __init__(self, coord: Coordinator):
        self.c = coord

    # -- entry points ---------------------------------------------------------------------------------
    def seen(self, channel: str, update_id: str | None, sender: str, accepted: bool) -> bool:
        """Record an inbound update; returns True if it was already processed (duplicate delivery)."""
        if update_id is None:
            return False
        cur = self.c.db.execute(
            "INSERT OR IGNORE INTO inbound_updates(channel, update_id, sender, accepted, received_at) VALUES (?,?,?,?,?)",
            (channel, str(update_id), sender, int(accepted), now_iso()),
        )
        return cur.rowcount == 0

    async def handle_message(self, identity: Identity, text: str, *, update_id: str | None = None,
                             reply_to_message_id: str | None = None,
                             attachments: list[tuple[str, bytes]] | None = None,
                             conversation_id: str | None = None) -> str | None:
        text = (text or "").strip()
        if text.startswith("/pair"):
            return self._pair(identity, text)
        owner = self.c.is_owner(identity)
        if self.seen(identity.channel, update_id, identity.user_id, owner):
            return None
        if not owner:
            self.c.tasks.event(None, "unauthorized_message", {"channel": identity.channel, "sender": identity.user_id,
                                                              "text": truncate(text, 80)})
            return None
        if not text and not attachments:
            return None
        if text.startswith("/"):
            return await self._command(identity, text, attachments or [], conversation_id)
        if reply_to_message_id:
            tid = self._task_for_message(reply_to_message_id)
            t = self.c.tasks.get(tid) if tid else None
            if t is not None and not t.terminal:
                if t.status == TaskStatus.AWAITING_USER:
                    self.c.answer_task(t.id, text)
                    return f"Answer passed to task {t.id[-6:]}."
                self.c.steer_task(t.id, text)
                return None
        return self._new_task(text, "auto", attachments or [], conversation_id=conversation_id)

    async def handle_callback(self, identity: Identity, data: str, *, callback_id: str | None = None) -> str:
        if self.seen(f"{identity.channel}:cb", callback_id, identity.user_id, self.c.is_owner(identity)):
            return "Already handled."
        m = re.fullmatch(r"omca:apr:(apr_[A-Z0-9]+):([yn])", data or "")
        if not m:
            return "Unknown button."
        # approvals authenticate the approver separately from ordinary bot access
        return self.c.decide_approval(m.group(1), identity, "approve" if m.group(2) == "y" else "deny")

    # -- helpers ------------------------------------------------------------------------------------------
    def _task_for_message(self, message_id: str) -> str | None:
        for r in self.c.db.query("SELECT task_id, result_json FROM outbox WHERE status='SENT' AND task_id IS NOT NULL "
                                 "ORDER BY sent_at DESC LIMIT 500"):
            if str((loads(r["result_json"], {}) or {}).get("message_id")) == str(message_id):
                return r["task_id"]
        return None

    def _resolve(self, ref: str) -> Any:
        t = self.c.tasks.by_short_id(ref) if ref else None
        if t is None and not ref:
            active = self.c.tasks.active()
            t = active[0] if len(active) == 1 else None
        return t

    def _new_task(self, goal: str, workflow: str, attachments: list[tuple[str, bytes]], params: dict | None = None,
                  conversation_id: str | None = None) -> str:
        req = TaskRequest(goal=goal[:8000], workflow=workflow, params=params or {},  # type: ignore[arg-type]
                          conversation_id=conversation_id,
                          sources=[u.rstrip(").,") for u in URL.findall(goal)][:10])
        task = self.c.submit_task(req)
        if attachments:
            ids = []
            for name, data in attachments[:10]:
                try:
                    ids.append(self.c.artifacts.put(task.id, "input", name, data).manifest.artifact_id)
                except Exception as exc:  # noqa: BLE001
                    self.c.notifier.text(f"Attachment {name} was not accepted: {exc}", task_id=task.id)
            self.c.tasks.update(task.id, request=req.model_copy(update={"attachments": ids}))
        return None  # the acknowledgement goes through the outbox

    def _pair(self, identity: Identity, text: str) -> str | None:
        if identity.channel != "telegram":
            return None
        if self.c.notifier.owner_ids():
            return None  # already paired or configured: silently ignore
        code = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        pending = self.c.db.kv_get("pairing")
        if not pending or not code or code != pending.get("code"):
            return None
        if parse_iso(pending["expires_at"]) < utcnow():  # type: ignore[operator]
            self.c.db.kv_set("pairing", None)
            return None
        self.c.db.kv_set("paired_owner_id", int(identity.user_id))
        self.c.db.kv_set("pairing", None)
        self.c.tasks.event(None, "owner_paired", {"user_id": identity.user_id})
        return "Paired. This chat is now the only owner of this assistant. Send /help for commands."

    def start_pairing(self) -> str:
        code = random_token(6)
        ttl = self.c.config.telegram.pairing_code_ttl_seconds
        self.c.db.kv_set("pairing", {"code": code, "expires_at": iso(utcnow() + timedelta(seconds=ttl))})
        return code

    async def _command(self, identity: Identity, text: str, attachments: list[tuple[str, bytes]],
                       conversation_id: str | None = None) -> str | None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        rest = rest.strip()
        c = self.c
        if cmd == "/omca":
            sub, _, more = rest.partition(" ")
            if not sub:
                return HELP
            return await self._command(identity, f"/{sub} {more}".strip(), attachments, conversation_id)
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/general":
            return self._new_task(rest, "general_workflow", attachments, conversation_id=conversation_id) if rest else "Usage: /general <message>"
        if cmd == "/research":
            return self._new_task(rest, "research_report", attachments, conversation_id=conversation_id) if rest else "Usage: /research <topic>"
        if cmd == "/video":
            m = URL.search(rest)
            question = URL.sub("", rest).strip() or "Summarise what happens in the video with timestamps."
            params = {"url": m.group(0)} if m else {}
            if m:
                params["question"] = question
            return self._new_task(rest or "Analyse a video", "video_analysis", attachments, params, conversation_id)
        if cmd == "/browse":
            return self._new_task(rest, "browser_task", attachments, conversation_id=conversation_id) if rest else "Usage: /browse <goal>"
        if cmd == "/office":
            return self._new_task(rest, "desktop_workflow", attachments, conversation_id=conversation_id) if rest else "Usage: /office <goal>"
        if cmd == "/tasks":
            items = c.tasks.list(limit=10)
            return "\n".join(f"{t.id[-6:]} {t.status.value.lower():<17} {truncate(t.goal, 60)}" for t in items) or "No tasks yet."
        if cmd == "/status":
            t = self._resolve(rest)
            if t is None:
                return "Which task? Use /tasks to list them."
            return c.status_text(t)
        if cmd == "/pause":
            if rest.lower() == "all":
                for t in c.tasks.active():
                    try:
                        c.pause_task(t.id)
                    except TransitionError:
                        pass
                c.scheduler.pause_all()
                return "All active tasks and schedules are paused."
            t = self._resolve(rest)
            if t is None:
                return "Which task? Use /tasks."
            c.pause_task(t.id)
            return None
        if cmd == "/resume":
            parts = rest.split()
            t = self._resolve(parts[0] if parts else "")
            if t is None:
                return "Which task? Use /tasks."
            extra = None
            if len(parts) > 1 and parts[1].startswith("+"):
                try:
                    extra = float(parts[1][1:])
                except ValueError:
                    return "Budget increase must look like +50"
            c.resume_task(t.id, extra_budget=extra)
            return None
        if cmd == "/cancel":
            t = self._resolve(rest)
            if t is None:
                return "Which task? Use /tasks."
            c.cancel_task(t.id)
            return None
        if cmd in ("/steer", "/answer"):
            ref, _, body = rest.partition(" ")
            t = self._resolve(ref)
            if t is None or not body.strip():
                return f"Usage: {cmd} <task id> <text>"
            try:
                if cmd == "/answer":
                    c.answer_task(t.id, body.strip())
                    return f"Answer passed to task {t.id[-6:]}."
                c.steer_task(t.id, body.strip())
            except TransitionError as exc:
                return str(exc)
            return None
        if cmd in ("/approve", "/deny"):
            if not rest.startswith("apr_"):
                return f"Usage: {cmd} <approval id>"
            return c.decide_approval(rest.split()[0], identity, "approve" if cmd == "/approve" else "deny")
        if cmd == "/budget":
            st = c.ledger.status()
            lines = [f"Model budget {st.period} ({st.period_timezone}): {st.ceiling - st.remaining:.2f} of "
                     f"{st.ceiling:.2f} {st.currency} committed ({st.utilisation:.0%})",
                     f"settled {st.settled:.2f}, reserved {st.reserved:.2f}, unknown {st.unknown:.2f}",
                     f"Hosted routes enabled: {', '.join(st.hosted_routes_enabled) or 'none'}"]
            lines += [f"disabled {k}: {v}" for k, v in st.disabled_routes.items()]
            return "\n".join(lines)
        if cmd == "/prefs":
            parts = rest.split(maxsplit=2)
            if not parts:
                prefs = c.memory.preferences()
                return "\n".join(f"{k}: {v}" for k, v in prefs.items()) or "No preferences saved."
            if parts[0] == "set" and len(parts) == 3:
                c.memory.set_preference(parts[1], parts[2])
                return f"Saved preference {parts[1]}."
            if parts[0] == "del" and len(parts) >= 2:
                return "Deleted." if c.memory.delete_preference(parts[1]) else "No such preference."
            return "Usage: /prefs [set <key> <value> | del <key>]"
        if cmd == "/schedules":
            items = c.scheduler.list()
            tz = c.config.owner.display_timezone
            return "\n".join(f"{s['id']}: {s['name']} [{s['cron']} {s['timezone']}] "
                             f"{'paused' if s['paused'] else 'next ' + display_time(s['next_run'], tz)}"
                             for s in items) or "No schedules."
        if cmd == "/schedule":
            return self._schedule(rest)
        return "Unknown command. Send /help."

    def _schedule(self, rest: str) -> str:
        c = self.c
        parts = rest.split(maxsplit=1)
        if not parts:
            return "Usage: /schedule add <id> \"<cron>\" <goal> | pause <id> | resume <id> | run <id>"
        sub = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if sub in ("pause", "resume"):
            return ("Done." if c.scheduler.set_paused(arg.strip(), sub == "pause") else "No such schedule.")
        if sub == "run":
            try:
                rid = c.scheduler.fire_external(arg.strip(), None)
            except KeyError:
                return "No such schedule."
            return "Started." if rid else "Not started (paused, disabled, overlapping or already run this minute)."
        if sub == "add":
            m = re.match(r'(\S+)\s+"([^"]+)"\s+(.+)', arg)
            if not m:
                return 'Usage: /schedule add <id> "<min hour dom mon dow>" <goal text, may use {date}>'
            try:
                s = ScheduleConfig(id=m.group(1), name=m.group(1), cron=m.group(2), goal_template=m.group(3),
                                   timezone=c.config.owner.display_timezone,
                                   spending_limit=c.config.budget.per_task_reservation_ceiling)
                c.scheduler.add(s)
            except Exception as exc:  # noqa: BLE001
                return f"Schedule not added: {exc}"
            return f"Schedule {s.id} added ({s.cron}, {s.timezone}). Results will come to this chat."
        return "Unknown schedule command."

