"""Policy gate: every external action and model-facing tool goes through ``evaluate``."""

from __future__ import annotations

import fnmatch
import posixpath
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import ApplicationConfig, StandingRule
from ..contracts import (
    ActionCategory,
    ActionIntent,
    ErrorCode,
    PolicyDecision,
    PolicyVerdict,
)
from ..db import Database, dumps
from ..util import digest, now_iso, parse_iso, utcnow
from .operations import OPERATIONS, Classification, OperationSpec, PolicyContext, origin_of

POLICY_CODE_VERSION = "2026-10-12.1"

NEEDS_FRESH_OBSERVATION = {"browser.click", "browser.click_point", "browser.type", "browser.press", "browser.select",
                           "browser.download"}

ARTIFACT_ARGUMENTS = ("artifact_id", "draft_artifact_id", "input_artifact_id")

ALLOW_CATEGORIES = {ActionCategory.READ, ActionCategory.NAVIGATION, ActionCategory.DRAFT, ActionCategory.CREATE_OUTPUT}
ASK_CATEGORIES = {ActionCategory.CONSEQUENTIAL, ActionCategory.UNKNOWN_MUTATION}


class PolicyError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


class PolicyGate:
    def __init__(self, db: Database, config: ApplicationConfig):
        self.db = db
        self.config = config
        content = {
            "code": POLICY_CODE_VERSION,
            "operations": sorted(OPERATIONS),
            "standing_rules": [r.model_dump(mode="json") for r in config.policy.standing_rules],
            "contacts": [c.model_dump() for c in config.policy.contacts],
            "extra_terms": config.policy.extra_consequential_terms,
            "approval_expiry_minutes": config.limits.approval_expiry_minutes,
        }
        self.version = "pol_" + digest(content)[:16]
        db.execute(
            "INSERT OR IGNORE INTO policy_versions(version, content_json, created_at) VALUES (?,?,?)",
            (self.version, dumps(content), now_iso()),
        )

    # -- validation -----------------------------------------------------------
    @staticmethod
    def spec(operation: str) -> OperationSpec:
        spec = OPERATIONS.get(operation)
        if spec is None:
            raise PolicyError(ErrorCode.POLICY_DENIED, f"operation {operation!r} is not allowlisted")
        return spec

    def validate_args(self, operation: str, arguments: dict[str, Any]) -> BaseModel:
        spec = self.spec(operation)
        try:
            return spec.args.model_validate(arguments)
        except ValidationError as exc:
            raise PolicyError(ErrorCode.MALFORMED_TOOL_CALL, f"invalid arguments for {operation}: {exc.errors()[:3]}") from exc

    # -- evaluation -------------------------------------------------------------
    def evaluate(self, intent: ActionIntent, ctx: PolicyContext) -> tuple[PolicyDecision, Classification | None]:
        try:
            spec = self.spec(intent.operation)
            args = self.validate_args(intent.operation, intent.arguments)
        except PolicyError as exc:
            return self._decision(PolicyVerdict.DENY, ActionCategory.FORBIDDEN, str(exc), error=exc.code), None

        foreign = self._foreign_artifact(intent, ctx)
        if foreign:
            return self._decision(PolicyVerdict.DENY, ActionCategory.FORBIDDEN, foreign, error=ErrorCode.POLICY_DENIED), None

        if intent.operation in NEEDS_FRESH_OBSERVATION:
            stale = self._stale(intent, ctx)
            if stale:
                return self._decision(PolicyVerdict.DENY, ActionCategory.READ, stale,
                                      error=ErrorCode.STALE_OBSERVATION), None

        cls = spec.classify(args, intent.target, ctx)
        if cls.category == ActionCategory.FORBIDDEN:
            return self._decision(PolicyVerdict.DENY, cls.category, cls.reason, destination=cls.destination,
                                  error=ErrorCode.POLICY_DENIED), cls
        if cls.category in ALLOW_CATEGORIES:
            return self._decision(PolicyVerdict.ALLOW, cls.category, cls.reason, destination=cls.destination,
                                  consequence=cls.consequence), cls
        rule = self.matching_rule(intent, cls, ctx)
        if rule is not None:
            return self._decision(PolicyVerdict.ALLOW, cls.category, f"{cls.reason}; allowed by standing rule {rule.id}",
                                  destination=cls.destination, consequence=cls.consequence, rule=rule.id), cls
        return self._decision(PolicyVerdict.REQUIRE_APPROVAL, cls.category, cls.reason, destination=cls.destination,
                              consequence=cls.consequence or cls.reason), cls

    @staticmethod
    def _foreign_artifact(intent: ActionIntent, ctx: PolicyContext) -> str | None:
        """Inputs must be this task's own outputs or files the owner attached to it."""
        if ctx.artifacts is None:
            return None
        for key in ARTIFACT_ARGUMENTS:
            aid = intent.arguments.get(key)
            if not isinstance(aid, str):
                continue
            if aid in (ctx.task_inputs or []):
                continue
            manifest = ctx.artifacts(aid)
            if manifest is None or manifest.task_id != (ctx.task_id or intent.task_id):
                return f"artifact {aid} does not belong to this task"
        return None

    def _stale(self, intent: ActionIntent, ctx: PolicyContext) -> str | None:
        obs = ctx.observation
        if obs is None:
            return "a fresh observation is required before this action"
        if intent.observation_id and intent.observation_id != obs.observation_id:
            return "the action refers to an older observation"
        captured = parse_iso(obs.captured_at)
        age = (utcnow() - captured).total_seconds() if captured else 1e9
        if age > self.config.limits.observation_max_age_seconds:
            return f"observation is {age:.0f}s old (max {self.config.limits.observation_max_age_seconds:.0f}s)"
        return None

    def matching_rule(self, intent: ActionIntent, cls: Classification, ctx: PolicyContext) -> StandingRule | None:
        now = utcnow()
        for rule in self.config.policy.standing_rules:
            if not fnmatch.fnmatchcase(intent.operation, rule.operation):
                continue
            if rule.expires_at and rule.expires_at.astimezone(now.tzinfo) <= now:
                continue
            if rule.max_uses is not None:
                used = self.db.scalar("SELECT COUNT(*) FROM standing_rule_uses WHERE rule_id=?", (rule.id,)) or 0
                if used >= rule.max_uses:
                    continue
            page_origin = origin_of(ctx.observation.url) if ctx.observation else None
            dest_origin = origin_of(cls.destination) or page_origin
            if rule.origin:
                pattern = rule.origin.lower()
                regex = "^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$"
                if not dest_origin or not re.match(regex, dest_origin) or (page_origin and not re.match(regex, page_origin)):
                    continue
            if rule.recipient and (cls.destination or "").lower() != rule.recipient.lower():
                continue
            if rule.path_prefix and not _path_within(cls.destination, rule.path_prefix):
                continue
            if rule.element_name_pattern:
                name = cls.element.name if cls.element else ""
                if not re.fullmatch(rule.element_name_pattern, name or "", flags=re.I):
                    continue
            return rule
        return None

    def record_rule_use(self, rule_id: str, action_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO standing_rule_uses(rule_id, action_id, used_at) VALUES (?,?,?)",
                        (rule_id, action_id, now_iso()))

    def _decision(self, verdict: PolicyVerdict, category: ActionCategory, reason: str, *, destination: str | None = None,
                  consequence: str | None = None, rule: str | None = None, error: ErrorCode | None = None) -> PolicyDecision:
        return PolicyDecision(verdict=verdict, category=category, reason=reason, policy_version=self.version,
                              destination=destination, consequence=consequence, standing_rule_id=rule, error_code=error)


def _path_within(destination: str | None, prefix: str) -> bool:
    """``destination`` starts with ``prefix`` after normalisation, so ``a/../b`` cannot escape a rule's folder."""
    if not destination:
        return False
    raw = destination.replace("\\", "/")
    if raw.startswith("/") or ".." in raw.split("/"):
        return False
    want = posixpath.normpath(prefix.replace("\\", "/")) + ("/" if prefix.endswith(("/", "\\")) else "")
    return posixpath.normpath(raw).startswith(want)
