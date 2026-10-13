"""Durable task execution (spec section 8; FR02, FR08, FR11, FR12, FR14).

Workflows are ordinary async functions over a ``TaskContext``. Every non-deterministic
operation (model call, observation, external action) is a *journaled step*: its output is
persisted when it completes and replayed instead of re-executed after a restart. External
actions additionally go through ``TaskContext.act``, the single governed execution path:

    persist intent -> policy gate -> (approval -> revalidate -> consume nonce) -> limits ->
    lease + fencing -> persist DISPATCHED -> execute -> persist receipt -> reobserve/verify

Read-only operations may be retried within bounds; consequential operations whose outcome
is uncertain are reconciled with the worker and, failing that, the owner is asked. An
action never counts as successful because the model asked for it; only a receipt does.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .budget import BudgetBlocked
from .contracts import (
    ActionIntent,
    ActionReceipt,
    ActionStatus,
    ErrorCode,
    ExpectedResult,
    Observation,
    PolicyDecision,
    PolicyVerdict,
    ReceiptError,
    ReceiptStatus,
    TaskStatus,
    WorkerJob,
)
from .db import dumps, loads
from .models.router import ModelCallError
from .models.types import ImageInput, ModelResponse
from .policy import OPERATIONS, ApprovalError, PolicyContext
from .policy.operations import ElementRef, origin_of
from .tasks import TaskRecord, TransitionError
from .util import digest, new_id, now_iso
from .workers import WorkerError, job_deadline

if TYPE_CHECKING:
    from .coordinator import Coordinator

S = TaskStatus


class TaskInterrupted(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "pause" or "cancel"


class TaskLimitExceeded(Exception):
    code = ErrorCode.LIMIT_EXCEEDED


class TaskBlocked(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


class ActionFailure(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain


@dataclass
class ActionOutcome:
    action_id: str
    operation: str
    status: ActionStatus
    category: str | None = None
    verdict: str | None = None
    reason: str | None = None
    error_code: str | None = None
    message: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    approval_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == ActionStatus.SUCCEEDED

    @property
    def obs(self) -> Observation | None:
        return Observation.model_validate(self.observation) if self.observation else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ActionOutcome:
        return ActionOutcome(**{**d, "status": ActionStatus(d["status"])})


@dataclass
class WorkflowOutcome:
    status: str  # COMPLETED, PARTIAL, FAILED
    summary: str
    artifacts: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    checks_passed: bool = False
    error_code: str | None = None


Workflow = Callable[["TaskContext"], Awaitable[WorkflowOutcome]]


class TaskContext:
    def __init__(self, coord: Coordinator, task: TaskRecord, interrupt: asyncio.Event):
        self.c = coord
        self.task = task
        self.interrupt = interrupt
        self._active_started = time.monotonic()
        self._leases: dict[str, Any] = {}
        self._lease_task: asyncio.Task | None = None
        self.limitations: list[str] = []
        # set on every runner (re)start: the first live observation must re-check the real page state
        self.needs_live_check = True
        # increments on every runner (re)start; keys live-state checks so a resumed run never reuses old ones
        key = f"run_epoch:{task.id}"
        self.run_epoch = int(coord.db.kv_get(key, 0)) + 1
        coord.db.kv_set(key, self.run_epoch)

    # -- identity / helpers -------------------------------------------------------------------
    @property
    def task_id(self) -> str:
        return self.task.id

    @property
    def short(self) -> str:
        return self.task.id[-6:]

    @property
    def config(self):
        return self.c.config

    def refresh(self) -> TaskRecord:
        self.task = self.c.tasks.require(self.task.id)
        return self.task

    def steering(self) -> list[str]:
        return self.c.tasks.steering(self.task.id)

    def task_ceiling(self) -> float | None:
        return self.task.request.budget.max_model_spend

    # -- interruption, limits, waits ---------------------------------------------------------------
    def check(self) -> None:
        t = self.refresh()
        if t.status == S.CANCELLED:
            raise TaskInterrupted("cancel")
        if t.status == S.PAUSED:
            raise TaskInterrupted("pause")
        self._check_time()

    def _flush_active(self) -> int:
        now = time.monotonic()
        ms = int((now - self._active_started) * 1000)
        self._active_started = now
        return self.c.tasks.add_active_ms(self.task.id, ms)

    def _check_time(self) -> None:
        total = self._flush_active()
        limit = (self.task.request.budget.max_active_minutes or self.config.limits.max_task_active_minutes) * 60_000
        if total > limit:
            raise TaskLimitExceeded(f"task used {total / 60000:.1f} active minutes (limit {limit / 60000:.0f})")

    async def _wait(self, aw: Awaitable[Any]) -> Any:
        """Wait for an owner decision or input. Waiting time is not counted as active time."""
        self._flush_active()
        fut = asyncio.ensure_future(aw)
        try:
            while True:
                stop = asyncio.ensure_future(self.interrupt.wait())
                try:
                    done, _ = await asyncio.wait({fut, stop}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stop.cancel()
                if fut in done:
                    return fut.result()
                self.interrupt.clear()
                t = self.refresh()
                if t.status in (S.CANCELLED, S.PAUSED):
                    fut.cancel()
                    raise TaskInterrupted("cancel" if t.status == S.CANCELLED else "pause")
        finally:
            self._active_started = time.monotonic()

    def set_status(self, status: TaskStatus, reason: str | None = None) -> None:
        try:
            self.c.tasks.transition(self.task.id, status, reason=reason)
        except TransitionError:
            self.check()
            raise
        self.refresh()
        self.c.on_status_change(self.task, reason)

    # -- journal ---------------------------------------------------------------------------------
    def _journal(self, key: str) -> Any | None:
        r = self.c.db.one("SELECT status, output_json FROM task_steps WHERE task_id=? AND step_key=?", (self.task.id, key))
        if r and r["status"] == "DONE":
            return loads(r["output_json"])
        return None

    def _journal_start(self, key: str) -> int:
        with self.c.db.tx() as c:
            r = c.execute("SELECT attempt FROM task_steps WHERE task_id=? AND step_key=?", (self.task.id, key)).fetchone()
            if r is None:
                c.execute("INSERT INTO task_steps(task_id, step_key, status, attempt, started_at) VALUES (?,?,?,1,?)",
                          (self.task.id, key, "STARTED", now_iso()))
                return 1
            c.execute("UPDATE task_steps SET status='STARTED', attempt=attempt+1, started_at=? WHERE task_id=? AND step_key=?",
                      (now_iso(), self.task.id, key))
            return r["attempt"] + 1

    def _journal_done(self, key: str, output: Any) -> None:
        self.c.db.execute("UPDATE task_steps SET status='DONE', output_json=?, finished_at=? WHERE task_id=? AND step_key=?",
                          (dumps(output), now_iso(), self.task.id, key))

    def _journal_fail(self, key: str, error: str) -> None:
        self.c.db.execute("UPDATE task_steps SET status='FAILED', error_json=?, finished_at=? WHERE task_id=? AND step_key=?",
                          (dumps({"error": error}), now_iso(), self.task.id, key))

    async def step(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run a read-only/deterministic-output step once; replay its stored output afterwards."""
        stored = self._journal(key)
        if stored is not None:
            return stored
        self.check()
        self._journal_start(key)
        try:
            out = await fn()
        except BaseException as exc:
            self._journal_fail(key, f"{type(exc).__name__}: {exc}")
            raise
        self._journal_done(key, out)
        return out

    # -- progress ----------------------------------------------------------------------------------
    def progress(self, text: str, *, phase: str | None = None) -> None:
        fields: dict[str, Any] = {"current_step": text[:300]}
        if phase:
            fields["phase"] = phase
        self.c.tasks.update(self.task.id, **fields)
        self.c.tasks.event(self.task.id, "progress", {"text": text, "phase": phase})

    def note_limitation(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)
            self.c.tasks.event(self.task.id, "limitation", {"text": text})

    # -- model calls ----------------------------------------------------------------------------------
    async def model(self, role: str, messages: list[dict[str, Any]], *, key: str, images: list[ImageInput] | None = None,
                    tools: list[dict[str, Any]] | None = None, tool_choice: Any = None,
                    max_output_tokens: int | None = None, temperature: float | None = 0.2) -> ModelResponse:
        async def run() -> dict[str, Any]:
            resp = await self.c.router.call(
                role, messages, images=images, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens,
                temperature=temperature, task_id=self.task.id, task_ceiling=self.task_ceiling(), purpose=role,
                caller=f"task:{self.task.workflow}",
            )
            return resp.to_dict()

        return ModelResponse.from_dict(await self.step(f"model:{key}", run))

    # -- owner input ---------------------------------------------------------------------------------
    async def ask_owner(self, question: str, *, key: str, timeout_hours: float | None = None) -> str | None:
        async def run() -> dict[str, Any]:
            self.c.notifier.text(f"Task {self.short} needs your input:\n{question}\n\nReply with /answer {self.short} <text>.",
                                 task_id=self.task.id, key=f"ask:{self.task.id}:{key}")
            self.set_status(S.AWAITING_USER, question[:300])
            hours = timeout_hours or self.config.limits.owner_input_timeout_hours

            async def poll() -> str | None:
                deadline = time.monotonic() + hours * 3600
                while time.monotonic() < deadline:
                    msgs = self.c.tasks.take_messages(self.task.id, "owner_input")
                    if msgs:
                        return msgs[-1]
                    await asyncio.sleep(0.5 if self.config.testing else 2.0)
                return None

            answer = await self._wait(poll())
            self.set_status(S.RUNNING, None)
            return {"answer": answer}

        return (await self.step(f"ask:{key}", run))["answer"]

    # -- leases ----------------------------------------------------------------------------------------
    def _lease(self, worker_id: str, resource: str):
        lease = self._leases.get(resource)
        if lease is not None:
            renewed = self.c.workers.renew_lease(lease.lease_id)
            if renewed is not None:
                self._leases[resource] = renewed
                return renewed
        lease = self.c.workers.acquire_lease(self.task.id, worker_id, resource)
        self._leases[resource] = lease
        self.c.tasks.update(self.task.id, worker_id=worker_id)
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.ensure_future(self._renew_loop())
        return lease

    async def _renew_loop(self) -> None:
        while self._leases:
            await asyncio.sleep(max(1.0, self.config.limits.worker_heartbeat_seconds))
            for res, lease in list(self._leases.items()):
                renewed = self.c.workers.renew_lease(lease.lease_id)
                if renewed is None:
                    self._leases.pop(res, None)
                else:
                    self._leases[res] = renewed

    def release_leases(self) -> None:
        for lease in self._leases.values():
            self.c.workers.release_lease(lease.lease_id)
        self._leases.clear()
        if self._lease_task is not None:
            self._lease_task.cancel()
        self.c.workers.release_task_leases(self.task.id)

    # -- the governed action path --------------------------------------------------------------------
    async def act(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        key: str,
        observation: Observation | None = None,
        expected: ExpectedResult | None = None,
        attachment_id: str | None = None,
        worker_id: str | None = None,
        preview: str | None = None,
        rationale: str | None = None,
        retries: int | None = None,
    ) -> ActionOutcome:
        stored = self._journal(f"act:{key}")
        if stored is not None:
            return ActionOutcome.from_dict(stored)
        self.check()
        spec = OPERATIONS.get(operation)
        max_attempts = 1 + (self.config.limits.max_retries_per_action if retries is None else retries)
        attempt = 0
        outcome: ActionOutcome | None = None
        while attempt < max_attempts:
            sub = key if attempt == 0 else f"{key}~{attempt}"
            outcome = await self._act_once(operation, arguments, sub, observation, expected, attachment_id, worker_id,
                                           preview, rationale)
            attempt += 1
            retryable = (spec is not None and spec.retry_safe and outcome.status == ActionStatus.FAILED
                         and (outcome.receipt or {}).get("error", {}) and (outcome.receipt or {})["error"].get("retryable"))
            if not retryable:
                break
            await asyncio.sleep(0 if self.config.testing else min(4.0, 1.0 * attempt))
        assert outcome is not None
        self.c.db.execute("INSERT OR IGNORE INTO task_steps(task_id, step_key, status, attempt, started_at) VALUES (?,?,?,?,?)",
                          (self.task.id, f"act:{key}", "STARTED", attempt, now_iso()))
        self._journal_done(f"act:{key}", outcome.to_dict())
        return outcome

    def _action_row(self, idem: str) -> Any:
        return self.c.db.one("SELECT * FROM actions WHERE idempotency_key=?", (idem,))

    def _set_action(self, action_id: str, **fields: Any) -> None:
        cols = []
        params: list[Any] = []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            params.append(v.value if hasattr(v, "value") else v)
        cols.append("updated_at=?")
        params.append(now_iso())
        self.c.db.execute(f"UPDATE actions SET {', '.join(cols)} WHERE id=?", (*params, action_id))

    def _outcome_from_row(self, row: Any, *, message: str | None = None) -> ActionOutcome:
        receipt = loads(row["receipt_json"])
        result = (receipt or {}).get("result") or {}
        err = (receipt or {}).get("error") or {}
        return ActionOutcome(
            action_id=row["id"], operation=row["operation"], status=ActionStatus(row["status"]),
            category=row["category"], verdict=row["verdict"], reason=row["policy_reason"],
            error_code=err.get("code"), message=message or err.get("message"), result=result, receipt=receipt,
            observation=(receipt or {}).get("observation"), verification=(receipt or {}).get("verification") or {},
            approval_id=row["approval_id"],
        )

    async def _act_once(self, operation, arguments, key, observation, expected, attachment_id, worker_id, preview,
                        rationale) -> ActionOutcome:
        idem = f"{self.task.id}:{key}"
        row = self._action_row(idem)
        spec = OPERATIONS.get(operation)
        if row is None:
            action_id = new_id("act")
            now = now_iso()
            self.c.db.execute(
                """INSERT INTO actions(id, task_id, step_key, worker_id, observation_id, operation, target_json,
                   arguments_json, arguments_hash, expected_json, idempotency_key, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (action_id, self.task.id, key, worker_id, observation.observation_id if observation else None, operation,
                 dumps({"attachment_id": attachment_id}), dumps(arguments), digest(arguments),
                 expected.model_dump_json() if expected else None, idem, ActionStatus.INTENDED.value, now, now),
            )
            self.c.tasks.event(self.task.id, "action_intent", {"action_id": action_id, "operation": operation,
                                                               "rationale": rationale})
            row = self._action_row(idem)
        action_id = row["id"]
        status = ActionStatus(row["status"])
        if status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.DENIED, ActionStatus.CANCELLED):
            return self._outcome_from_row(row)
        if status in (ActionStatus.DISPATCHED, ActionStatus.UNKNOWN):
            recovered = await self._recover(row, spec)
            if recovered is not None:
                return recovered
            row = self._action_row(idem)

        intent = ActionIntent(action_id=action_id, task_id=self.task.id, worker_id=worker_id,
                              observation_id=observation.observation_id if observation else None, operation=operation,
                              arguments=arguments, expected=expected, idempotency_key=idem, rationale=rationale)
        if spec is None:
            self._set_action(action_id, status=ActionStatus.DENIED, verdict="DENY", category="FORBIDDEN",
                             policy_reason=f"operation {operation} is not allowlisted")
            return self._outcome_from_row(self._action_row(idem))

        attachment = self.c.attachment(attachment_id) if attachment_id else None
        if row["approval_id"]:
            # resumed while an approval was pending (e.g. after a restart): keep the original decision
            ap = self.c.approvals.get(row["approval_id"])
            decision = PolicyDecision(verdict=PolicyVerdict.REQUIRE_APPROVAL, category=row["category"],
                                      reason=row["policy_reason"] or "", policy_version=row["policy_version"],
                                      destination=row["destination"], consequence=ap.consequence if ap else None)
            fp = (ap.preconditions.get("fingerprint") if ap else None)
            gated = await self._approve(intent, decision, None, fp, observation, attachment_id, worker_id, preview,
                                        row["approval_id"])
            if isinstance(gated, ActionOutcome):
                return gated
            intent, observation, fingerprint, approval_id = gated
            return await self._dispatch_approved(intent, spec, observation, fingerprint, attachment_id, worker_id,
                                                 approval_id)
        hit: ElementRef | None = None
        if operation == "browser.click_point":
            hit_out = await self.act("browser.hit_test", {"x": arguments["x"], "y": arguments["y"]}, key=f"{key}:hit",
                                     attachment_id=attachment_id, worker_id=worker_id)
            if hit_out.ok and hit_out.result.get("element"):
                hit = ElementRef.model_validate(hit_out.result["element"])
        decision, cls = self.c.policy.evaluate(intent, self._policy_context(observation, attachment, hit=hit))
        self._set_action(action_id, category=decision.category.value, verdict=decision.verdict.value,
                         policy_reason=decision.reason, policy_version=decision.policy_version,
                         destination=decision.destination,
                         consequential=int(decision.verdict == PolicyVerdict.REQUIRE_APPROVAL))
        self.c.tasks.event(self.task.id, "policy", {"action_id": action_id, "operation": operation,
                                                    "verdict": decision.verdict.value, "category": decision.category.value,
                                                    "reason": decision.reason})
        if decision.verdict == PolicyVerdict.DENY:
            self._set_action(action_id, status=ActionStatus.DENIED,
                             receipt_json=dumps({"error": {"code": (decision.error_code or ErrorCode.POLICY_DENIED).value,
                                                           "message": decision.reason}}))
            return self._outcome_from_row(self._action_row(idem))

        element = cls.element if cls else None
        fingerprint = element.fingerprint if element else None
        approval_id = None
        if decision.verdict == PolicyVerdict.REQUIRE_APPROVAL:
            gated = await self._approve(intent, decision, element, fingerprint, observation, attachment_id, worker_id,
                                        preview, None)
            if isinstance(gated, ActionOutcome):
                return gated
            intent, observation, fingerprint, approval_id = gated
        elif decision.standing_rule_id:
            self.c.policy.record_rule_use(decision.standing_rule_id, action_id)
        return await self._dispatch_approved(intent, spec, observation, fingerprint, attachment_id, worker_id, approval_id)

    async def _dispatch_approved(self, intent: ActionIntent, spec, observation, fingerprint, attachment_id, worker_id,
                                 approval_id) -> ActionOutcome:
        action_id, operation, idem = intent.action_id, intent.operation, intent.idempotency_key
        count = self.c.tasks.add_action_count(self.task.id)
        max_actions = self.task.request.budget.max_actions or self.config.limits.max_actions_per_task
        if count > max_actions:
            self._set_action(action_id, status=ActionStatus.CANCELLED)
            raise TaskLimitExceeded(f"task reached its limit of {max_actions} executed actions")

        receipt = await self._execute(intent, spec, observation, fingerprint, attachment_id, worker_id, approval_id)
        status = {ReceiptStatus.SUCCEEDED: ActionStatus.SUCCEEDED, ReceiptStatus.FAILED: ActionStatus.FAILED,
                  ReceiptStatus.REJECTED: ActionStatus.FAILED, ReceiptStatus.UNKNOWN: ActionStatus.UNKNOWN}[receipt.status]
        if receipt.observation is not None:
            self.c.observations.save(receipt.observation)
        self._set_action(action_id, status=status, receipt_json=receipt.model_dump_json())
        self.c.tasks.event(self.task.id, "action_receipt", {"action_id": action_id, "operation": operation,
                                                           "status": receipt.status.value,
                                                           "error": receipt.error.model_dump() if receipt.error else None})
        if status == ActionStatus.UNKNOWN and not spec.retry_safe:
            resolved = await self._resolve_uncertain(self._action_row(idem), spec)
            if resolved is not None:
                return resolved
        return self._outcome_from_row(self._action_row(idem))

    def _policy_context(self, observation, attachment, *, hit: ElementRef | None = None) -> PolicyContext:
        return PolicyContext(self.config, observation, hit_test=hit,
                             attachment_private_origins=attachment.allowed_private_origins if attachment else None,
                             task_id=self.task.id, artifacts=self.c.artifacts.get,
                             task_inputs=list(self.task.request.attachments))

    # -- approval -------------------------------------------------------------------------------------
    def _preconditions(self, operation: str, arguments: dict[str, Any], element: ElementRef | None,
                       observation: Observation | None) -> dict[str, Any]:
        if operation.startswith("browser."):
            return {"origin": origin_of(observation.url) if observation else None,
                    "url": observation.url.split("#", 1)[0] if observation else None,
                    "fingerprint": element.fingerprint if element else None,
                    "role": element.role if element else None, "name": element.name if element else None}
        return self.c.target_state(operation, arguments)

    async def _approve(self, intent: ActionIntent, decision: PolicyDecision, element: ElementRef | None,
                       fp: str | None, observation, attachment_id, worker_id, preview, approval_id):
        if approval_id is None:
            text = preview or self.c.describe_action(intent, decision, element)
            ap = self.c.approvals.create(
                intent, owner=self.c.owner_key, policy_version=decision.policy_version, destination=decision.destination,
                consequence=decision.consequence or decision.reason, preview=text,
                preconditions=self._preconditions(intent.operation, intent.arguments, element, observation),
                fingerprint=fp,
            )
            approval_id = ap.approval_id
            self._set_action(intent.action_id, status=ActionStatus.AWAITING_APPROVAL, approval_id=approval_id)
            self.c.notifier.approval(ap, self.short, approve_cmd=self.c.cmd("approve"), deny_cmd=self.c.cmd("deny"))
        if self.refresh().status != S.AWAITING_APPROVAL:
            self.set_status(S.AWAITING_APPROVAL, f"waiting for approval {approval_id}")
        # do not hold the UI while waiting; state is revalidated after the decision
        self.release_leases()
        ap = await self._wait(self.c.approvals.wait(approval_id, poll_seconds=0.5 if self.config.testing else 5.0))
        self.set_status(S.RUNNING, None)
        if ap.status.value != "APPROVED":
            code = ErrorCode.APPROVAL_EXPIRED if ap.status.value == "EXPIRED" else ErrorCode.APPROVAL_DENIED
            self._set_action(intent.action_id, status=ActionStatus.DENIED,
                             receipt_json=dumps({"error": {"code": code.value, "message": f"approval {ap.status.value.lower()}"}}))
            return self._outcome_from_row(self._action_row(intent.idempotency_key))
        # revalidate the current target before consuming the one-use approval
        fresh_obs = observation
        fresh_args = dict(intent.arguments)
        current_element = element
        op = intent.operation
        is_browser = op.startswith("browser.")
        if is_browser and op != "browser.navigate":
            ep = self.run_epoch
            obs_out = await self.act("browser.observe", {"include_images": False},
                                     key=f"{intent.idempotency_key}:reval:{ep}", attachment_id=attachment_id,
                                     worker_id=worker_id)
            fresh_obs = obs_out.obs
            if op == "browser.click_point":
                hit = await self.act("browser.hit_test", {"x": intent.arguments["x"], "y": intent.arguments["y"]},
                                     key=f"{intent.idempotency_key}:rehit:{ep}", attachment_id=attachment_id,
                                     worker_id=worker_id)
                current_element = ElementRef.model_validate(hit.result["element"]) if hit.ok and hit.result.get("element") else None
            elif op == "browser.press":
                # a key press acts on whatever has focus now, so the focused element must be the approved one
                current_element = None
                if fresh_obs is not None and fresh_obs.focused_ref:
                    current_element = next((e for e in fresh_obs.elements if e.ref == fresh_obs.focused_ref), None)
            elif fresh_obs is not None and fp:
                current_element = next((e for e in fresh_obs.elements if e.fingerprint == fp), None)
                if current_element is not None and "ref" in fresh_args:
                    fresh_args["ref"] = current_element.ref
        fresh_intent = intent.model_copy(update={"arguments": fresh_args,
                                                 "observation_id": fresh_obs.observation_id if fresh_obs else None})
        # classify again against the live state: the destination and verdict must still hold
        attachment = self.c.attachment(attachment_id) if attachment_id else None
        live_ctx = self._policy_context(fresh_obs, attachment,
                                        hit=current_element if op == "browser.click_point" else None)
        live_decision, _ = self.c.policy.evaluate(fresh_intent, live_ctx)
        now_pre = self._preconditions(op, fresh_args, current_element, fresh_obs)
        try:
            if live_decision.verdict == PolicyVerdict.DENY:
                self.c.approvals.invalidate(approval_id, f"no longer permitted: {live_decision.reason}")
                raise ApprovalError(ErrorCode.APPROVAL_INVALID, f"the action is no longer permitted ({live_decision.reason})")
            self.c.approvals.consume(approval_id, fresh_intent, owner=self.c.owner_key, fingerprint=fp,
                                     destination=live_decision.destination, current_preconditions=now_pre,
                                     current_policy_version=self.c.policy.version)
        except ApprovalError as exc:
            self._set_action(intent.action_id, status=ActionStatus.DENIED,
                             receipt_json=dumps({"error": {"code": exc.code.value, "message": str(exc)}}))
            self.c.notifier.text(f"Task {self.short}: approved action was not executed - {exc}.", task_id=self.task.id,
                                 key=f"apr-invalid:{approval_id}")
            return self._outcome_from_row(self._action_row(intent.idempotency_key))
        return fresh_intent, fresh_obs, fp, approval_id

    # -- execution -------------------------------------------------------------------------------------
    async def _execute(self, intent: ActionIntent, spec, observation, fingerprint, attachment_id, worker_id,
                       approval_id) -> ActionReceipt:
        started = now_iso()
        if spec.executor == "coordinator":
            self._set_action(intent.action_id, status=ActionStatus.DISPATCHED)
            try:
                result = await self.c.execute_local(intent.operation, intent.arguments, self)
                return ActionReceipt(action_id=intent.action_id, status=ReceiptStatus.SUCCEEDED, started_at=started,
                                     finished_at=now_iso(), result=result)
            except ActionFailure as exc:
                return ActionReceipt(action_id=intent.action_id,
                                     status=ReceiptStatus.UNKNOWN if exc.uncertain else ReceiptStatus.FAILED,
                                     started_at=started, finished_at=now_iso(),
                                     error=ReceiptError(code=exc.code, message=str(exc), retryable=exc.retryable))
        attachment = self.c.attachment(attachment_id) if attachment_id else None
        try:
            wid = self.c.workers.route(spec.capability,
                                       preferred=worker_id or (attachment.worker_id if attachment else None)
                                       or self.task.request.worker_id)
        except WorkerError as exc:
            raise TaskBlocked(exc.code, str(exc)) from exc
        resource = (attachment.lease_resource if attachment else f"{wid}:ui") if spec.needs_ui_lease else f"{wid}:task:{self.task.id}"
        try:
            lease = self._lease(wid, resource)
        except WorkerError as exc:
            raise TaskBlocked(exc.code, str(exc)) from exc
        args = dict(intent.arguments)
        if intent.observation_id:
            args["_obs"] = intent.observation_id
        if fingerprint:
            args["_fingerprint"] = fingerprint
        if intent.operation == "browser.press" and (fingerprint or approval_id):
            args["_focus_fp"] = fingerprint or "none"  # the worker refuses if focus moved
        if spec.retry_safe:
            # a read-only re-execution gets a fresh job; finished attempts are kept for the audit trail
            prior = self.c.workers.job_for_action(intent.action_id)
            if prior is not None and prior["status"] in ("DONE", "EXPIRED", "CANCELLED"):
                self.c.workers.supersede_job(prior["id"])
        timeout = 600.0 if intent.operation in ("media.fetch", "browser.video_frames", "browser.download") else 90.0
        if intent.operation == "browser.agent_task":
            timeout = self.config.browser.agent.max_minutes * 60 + 120  # agent time plus browser start and upload
        job = WorkerJob(job_id=new_id("job"), task_id=self.task.id, action_id=intent.action_id, worker_id=wid,
                        lease_id=lease.lease_id, resource=resource, fencing_token=lease.fencing_token,
                        operation=intent.operation, arguments=args, expected=intent.expected,
                        idempotency_key=intent.idempotency_key, deadline=job_deadline(timeout),
                        lease_expires_at=lease.expires_at, issued_at=now_iso(), approval_id=approval_id,
                        attachment_id=attachment_id,
                        declared_inputs=[v for k, v in intent.arguments.items()
                                         if k in ("artifact_id", "draft_artifact_id", "input_artifact_id") and isinstance(v, str)])
        self._set_action(intent.action_id, status=ActionStatus.DISPATCHED, worker_id=wid)
        dispatch = asyncio.ensure_future(self.c.workers.dispatch(job, timeout))
        stop = asyncio.ensure_future(self.interrupt.wait())
        try:
            done, _ = await asyncio.wait({dispatch, stop}, return_when=asyncio.FIRST_COMPLETED)
            if dispatch in done:
                return dispatch.result()
            # interrupted mid-flight: never claimed -> cancel; claimed -> record whatever completed
            self.c.workers.cancel_job(job.job_id)
            receipt = await dispatch
            if receipt.observation is not None:
                self.c.observations.save(receipt.observation)
            st = {ReceiptStatus.SUCCEEDED: ActionStatus.SUCCEEDED, ReceiptStatus.UNKNOWN: ActionStatus.UNKNOWN}.get(
                receipt.status, ActionStatus.FAILED)
            self._set_action(intent.action_id, status=st, receipt_json=receipt.model_dump_json())
            self.check()
            raise TaskInterrupted("pause")
        finally:
            stop.cancel()

    # -- recovery / reconciliation ----------------------------------------------------------------------
    async def _recover(self, row: Any, spec) -> ActionOutcome | None:
        """An action was dispatched before a restart or its outcome is uncertain."""
        receipt = self.c.workers.receipt_for_action(row["id"])
        if receipt is not None and receipt.status != ReceiptStatus.UNKNOWN:
            st = ActionStatus.SUCCEEDED if receipt.status == ReceiptStatus.SUCCEEDED else ActionStatus.FAILED
            if receipt.observation is not None:
                self.c.observations.save(receipt.observation)
            self._set_action(row["id"], status=st, receipt_json=receipt.model_dump_json())
            self.c.tasks.event(self.task.id, "reconciled", {"action_id": row["id"], "via": "stored receipt"})
            return self._outcome_from_row(self._action_row(row["idempotency_key"]))
        job = self.c.workers.job_for_action(row["id"])
        if job and job["status"] in ("PENDING", "CLAIMED"):
            # give a worker that restarted up to one lease period to return the receipt
            wj = WorkerJob.model_validate(loads(job["job_json"]))
            r = await self.c.workers.dispatch(wj, max(2.0, self.config.limits.lease_ttl_seconds))
            if r.status != ReceiptStatus.UNKNOWN:
                st = ActionStatus.SUCCEEDED if r.status == ReceiptStatus.SUCCEEDED else ActionStatus.FAILED
                self._set_action(row["id"], status=st, receipt_json=r.model_dump_json())
                return self._outcome_from_row(self._action_row(row["idempotency_key"]))
        if spec is not None and spec.retry_safe:
            # read-only: safe to execute again
            self._set_action(row["id"], status=ActionStatus.INTENDED)
            self.c.tasks.event(self.task.id, "reconciled", {"action_id": row["id"], "via": "read-only retry"})
            return None
        if spec is None or spec.executor == "coordinator":
            # coordinator-side effects (sends, deletions) have no job to consult: reconcile, never re-run
            return await self._resolve_uncertain(row, spec)
        if job is None:
            # never handed to a worker: nothing can have happened
            self._set_action(row["id"], status=ActionStatus.INTENDED)
            return None
        return await self._resolve_uncertain(row, spec)

    def _same_session(self, job: dict[str, Any], rec: ActionOutcome) -> bool:
        """'Never started' is only trustworthy from the worker process that claimed the original job.

        A different (newer) process cannot know whether an older one is about to act.
        """
        if job.get("claimed_epoch") is None:
            return job.get("status") in ("EXPIRED", "CANCELLED", "DONE")  # never claimed and no longer claimable
        recon = self.c.workers.job_for_action(rec.action_id)
        return recon is not None and recon.get("claimed_epoch") == job["claimed_epoch"]

    async def _resolve_uncertain(self, row: Any, spec) -> ActionOutcome | None:
        """Never repeat a consequential effect blindly: ask the worker, then the owner."""
        job = self.c.workers.job_for_action(row["id"])
        if job is not None and spec is not None and spec.executor == "worker":
            rec = await self.act("worker.reconcile", {"action_id": row["id"], "original_operation": row["operation"]},
                                 key=f"{row['step_key']}:reconcile", worker_id=job["worker_id"], retries=0)
            state = rec.result.get("state") if rec.ok else None
            if state == "finished":
                prior = ActionReceipt.model_validate(rec.result["receipt"])
                st = ActionStatus.SUCCEEDED if prior.status == ReceiptStatus.SUCCEEDED else (
                    ActionStatus.UNKNOWN if prior.status == ReceiptStatus.UNKNOWN else ActionStatus.FAILED)
                if st != ActionStatus.UNKNOWN:
                    self._set_action(row["id"], status=st, receipt_json=prior.model_dump_json())
                    self.c.tasks.event(self.task.id, "reconciled", {"action_id": row["id"], "via": "worker receipt cache"})
                    return self._outcome_from_row(self._action_row(row["idempotency_key"]))
            elif state == "never_started" and self._same_session(job, rec):
                self._set_action(row["id"], status=ActionStatus.FAILED,
                                 receipt_json=dumps({"status": "FAILED", "error": {
                                     "code": ErrorCode.WORKER_UNAVAILABLE.value, "retryable": True,
                                     "message": "the worker confirms the action never started"}}))
                self.c.tasks.event(self.task.id, "reconciled", {"action_id": row["id"], "via": "worker: never started"})
                return self._outcome_from_row(self._action_row(row["idempotency_key"]))
        # cannot establish what happened: the owner decides; never repeat automatically
        self._set_action(row["id"], status=ActionStatus.UNKNOWN)
        answer = await self.ask_owner(
            f"I could not confirm whether '{row['operation']}' (action {row['id'][-6:]}) completed. Please check and reply "
            f"'done' if it happened, 'not done' if it did not, or 'stop' to stop the task.",
            key=f"uncertain:{row['id']}",
        )
        a = (answer or "").strip().lower()
        if a.startswith("done") or a in {"yes", "happened"}:
            self._set_action(row["id"], status=ActionStatus.SUCCEEDED,
                             receipt_json=dumps({"status": "SUCCEEDED", "result": {"confirmed_by_owner": True}}))
        elif a.startswith("not"):
            self._set_action(row["id"], status=ActionStatus.FAILED,
                             receipt_json=dumps({"status": "FAILED", "error": {"code": ErrorCode.UNCERTAIN_SIDE_EFFECT.value,
                                                                                "message": "owner confirmed it did not happen"}}))
        else:
            self._set_action(row["id"], status=ActionStatus.CANCELLED)
            self.c.tasks.transition(self.task.id, S.CANCELLED, reason="owner stopped the task after an uncertain effect")
            raise TaskInterrupted("cancel")
        return self._outcome_from_row(self._action_row(row["idempotency_key"]))


class TaskEngine:
    def __init__(self, coord: Coordinator, workflows: dict[str, Workflow]):
        self.c = coord
        self.workflows = workflows
        self.runners: dict[str, asyncio.Task] = {}
        self.interrupts: dict[str, asyncio.Event] = {}

    def interrupt(self, task_id: str) -> None:
        ev = self.interrupts.get(task_id)
        if ev is not None:
            ev.set()

    def start(self, task_id: str) -> asyncio.Task:
        existing = self.runners.get(task_id)
        if existing is not None and not existing.done():
            return existing
        ev = asyncio.Event()
        self.interrupts[task_id] = ev
        t = asyncio.ensure_future(self._run(task_id, ev))
        self.runners[task_id] = t
        return t

    def schedule(self) -> None:
        running = {tid for tid, t in self.runners.items() if not t.done()}
        capacity = self.c.config.limits.max_concurrent_tasks - len(running)
        # recover tasks that were mid-flight when the process stopped
        for t in self.c.tasks.list([S.RUNNING, S.VERIFYING, S.AWAITING_APPROVAL, S.AWAITING_USER], limit=200):
            if t.id not in running:
                self.start(t.id)
                running.add(t.id)
        for t in sorted(self.c.tasks.list([S.QUEUED], limit=200), key=lambda x: x.created_at):
            if capacity <= 0:
                break
            if t.id not in running:
                self.start(t.id)
                capacity -= 1
        # periodically retry tasks blocked on an unavailable worker
        for t in self.c.tasks.list([S.BLOCKED], limit=200):
            if t.error_code == ErrorCode.WORKER_UNAVAILABLE.value and t.id not in running:
                age = time.time() - _ts(t.updated_at)
                if age > (5 if self.c.config.testing else 60):
                    try:
                        self.c.tasks.transition(t.id, S.QUEUED, reason="retrying after worker became unavailable")
                    except TransitionError:
                        pass

    async def run_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.schedule()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def _run(self, task_id: str, interrupt: asyncio.Event) -> None:
        tasks = self.c.tasks
        task = tasks.require(task_id)
        if task.terminal or task.status == S.PAUSED:
            return
        workflow = self.workflows.get(task.workflow)
        ctx = TaskContext(self.c, task, interrupt)
        try:
            if task.status in (S.QUEUED, S.BLOCKED):
                ctx.set_status(S.RUNNING, None)
            if workflow is None:
                raise ActionFailure(ErrorCode.UNSUPPORTED_CAPABILITY, f"no workflow named {task.workflow}")
            outcome = await workflow(ctx)
            ctx.check()
            final = self._final_status(outcome)
            if ctx.refresh().status != S.VERIFYING:
                ctx.set_status(S.VERIFYING, None)
            tasks.transition(task_id, final, error_code=outcome.error_code,
                             result={"summary": outcome.summary, "artifacts": outcome.artifacts,
                                     "limitations": outcome.limitations + [x for x in ctx.limitations
                                                                           if x not in outcome.limitations],
                                     "details": outcome.details, "checks_passed": outcome.checks_passed})
            self.c.on_finished(tasks.require(task_id))
        except TaskInterrupted as exc:
            self.c.on_interrupted(tasks.require(task_id), exc.kind)
        except TaskLimitExceeded as exc:
            self._finish_error(task_id, S.PARTIAL if self._has_progress(task_id) else S.FAILED, ErrorCode.LIMIT_EXCEEDED, str(exc), ctx)
        except BudgetBlocked as exc:
            if exc.scope == "task":
                self._finish_error(task_id, S.PARTIAL if self._has_progress(task_id) else S.FAILED,
                                   ErrorCode.BUDGET_BLOCKED, str(exc), ctx)
            else:
                self._block(task_id, ErrorCode.BUDGET_BLOCKED, str(exc))
        except TaskBlocked as exc:
            self._block(task_id, exc.code, str(exc))
        except ModelCallError as exc:
            self._finish_error(task_id, S.FAILED, exc.code, str(exc), ctx)
        except ActionFailure as exc:
            self._finish_error(task_id, S.FAILED, exc.code, str(exc), ctx)
        except TransitionError:
            t = tasks.require(task_id)
            if t.status == S.CANCELLED:
                self.c.on_interrupted(t, "cancel")
        except Exception as exc:  # noqa: BLE001 - a task never crashes the engine
            self._finish_error(task_id, S.FAILED, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}", ctx)
        finally:
            ctx.release_leases()
            final = self.c.tasks.get(task_id)
            if final is not None and final.terminal:
                # keep the tab while paused or blocked so its state can be revalidated on resume
                await self.c.release_browser(ctx)
            self.interrupts.pop(task_id, None)

    @staticmethod
    def _final_status(outcome: WorkflowOutcome) -> TaskStatus:
        if outcome.status == "COMPLETED" and outcome.checks_passed:
            return S.COMPLETED
        if outcome.status in ("COMPLETED", "PARTIAL"):
            return S.PARTIAL
        return S.FAILED

    def _has_progress(self, task_id: str) -> bool:
        return bool(self.c.db.scalar("SELECT COUNT(*) FROM evidence WHERE task_id=?", (task_id,)))

    def _finish_error(self, task_id: str, status: TaskStatus, code: ErrorCode, message: str, ctx: TaskContext) -> None:
        t = self.c.tasks.require(task_id)
        if t.terminal:
            return
        try:
            if t.status == S.PAUSED:
                self.c.tasks.transition(task_id, S.QUEUED)
                self.c.tasks.transition(task_id, S.RUNNING)
            self.c.tasks.transition(task_id, status, error_code=code,
                                    result={"summary": message, "limitations": ctx.limitations, "artifacts": [],
                                            "checks_passed": False})
        except TransitionError:
            return
        self.c.on_finished(self.c.tasks.require(task_id))

    def _block(self, task_id: str, code: ErrorCode, message: str) -> None:
        try:
            self.c.tasks.transition(task_id, S.BLOCKED, reason=message, error_code=code)
        except TransitionError:
            return
        self.c.on_status_change(self.c.tasks.require(task_id), message)


def _ts(iso_str: str) -> float:
    from .util import parse_iso

    dt = parse_iso(iso_str)
    return dt.timestamp() if dt else 0.0


def jdump(v: Any) -> str:
    return json.dumps(v, default=str)
