"""Application contracts (spec section 14).

These are the proposed application interfaces, not OpenClaw API schemas. The JSON
Schemas in ``contracts/schemas.json`` are generated from these models
(``omca contracts export``) and a test keeps them in sync.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_USER = "AWAITING_USER"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED})


class ErrorCode(StrEnum):
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA"
    MALFORMED_TOOL_CALL = "MALFORMED_TOOL_CALL"
    UNCERTAIN_SIDE_EFFECT = "UNCERTAIN_SIDE_EFFECT"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    STALE_ELEMENT_REF = "STALE_ELEMENT_REF"
    STALE_FENCING_TOKEN = "STALE_FENCING_TOKEN"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    ROUTE_REJECTED = "ROUTE_REJECTED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    SITE_UNAVAILABLE = "SITE_UNAVAILABLE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"


class ActionCategory(StrEnum):
    READ = "READ"                      # public research, screenshots, approved reads
    NAVIGATION = "NAVIGATION"          # tested navigation (links, tabs, scrolling)
    CREATE_OUTPUT = "CREATE_OUTPUT"    # new file in a dedicated output folder
    DRAFT = "DRAFT"                    # local preview or form field edits, no external effect
    CONSEQUENTIAL = "CONSEQUENTIAL"    # submit, send, purchase, delete, publish, overwrite, permission change
    UNKNOWN_MUTATION = "UNKNOWN_MUTATION"  # GUI mutation with unclear consequence
    FORBIDDEN = "FORBIDDEN"            # unexpected recipient, credential export, invalid token


class PolicyVerdict(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    INVALIDATED = "INVALIDATED"


class ActionStatus(StrEnum):
    INTENDED = "INTENDED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    DENIED = "DENIED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class ReceiptStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# Task request / view
# ---------------------------------------------------------------------------


WorkflowName = Literal["research_report", "video_analysis", "browser_task", "desktop_workflow", "chat_workflow", "auto"]


class TaskBudget(_C):
    max_model_spend: float | None = Field(default=None, description="Per-task hosted inference ceiling in budget currency")
    max_actions: int | None = None
    max_active_minutes: float | None = None


class TaskRequest(_C):
    """Created only from an authenticated owner context (channel bridge or owner API token)."""

    goal: str = Field(min_length=1, max_length=8000)
    workflow: WorkflowName = "auto"
    constraints: list[str] = Field(default_factory=list)
    output_format: Literal["docx", "docx+pdf", "message"] = "docx"
    budget: TaskBudget = Field(default_factory=TaskBudget)
    source_preference: Literal["auto", "api", "browser"] = "auto"
    sources: list[str] = Field(default_factory=list, description="URLs or domains to use or prefer")
    attachments: list[str] = Field(default_factory=list, description="Artifact IDs of owner-supplied inputs")
    worker_id: str | None = Field(default=None, description="Explicit job routing to a worker")
    params: dict[str, Any] = Field(default_factory=dict, description="Workflow parameters (e.g. video url, question)")
    conversation_id: str | None = Field(default=None, max_length=64, pattern=r"^cnv_[A-Za-z0-9]+$",
                                        description="Chat this task belongs to; chat replies only see their own conversation")


class TaskView(_C):
    task_id: str
    conversation_id: str | None = None
    status: TaskStatus
    workflow: str
    goal: str
    phase: str | None = None
    current_step: str | None = None
    worker_id: str | None = None
    blocking_reason: str | None = None
    progress: str | None = None
    created_at: str
    updated_at: str
    result: dict[str, Any] | None = None
    artifacts: list[ArtifactManifest] = Field(default_factory=list)
    pending_approval_id: str | None = None


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class Rect(_C):
    x: float
    y: float
    width: float
    height: float


class ElementRef(_C):
    ref: str = Field(description="Reference valid only for the observation that produced it")
    role: str
    name: str = ""
    tag: str
    rect: Rect
    href: str | None = None
    input_type: str | None = None
    in_form: bool = False
    form_id: str | None = None
    disabled: bool = False
    expanded: bool | None = None
    checked: bool | None = None
    value: str | None = None
    editable: bool = False
    fingerprint: str


class ImageRef(_C):
    artifact_id: str | None = None
    src: str | None = None
    alt: str | None = None
    natural_width: int | None = None
    natural_height: int | None = None
    rect: Rect | None = None


class Viewport(_C):
    width: int
    height: int
    device_pixel_ratio: float = 1.0
    scroll_x: float = 0.0
    scroll_y: float = 0.0


class Observation(_C):
    observation_id: str
    task_id: str | None = None
    worker_id: str
    session_id: str
    url: str
    title: str = ""
    captured_at: str
    viewport: Viewport
    elements: list[ElementRef] = Field(default_factory=list)
    focused_ref: str | None = Field(default=None, description="Element that currently has keyboard focus")
    text: str = Field(default="", description="Visible text (untrusted task data)")
    screenshot_artifact_id: str | None = None
    screenshot_sha256: str | None = None
    screenshot_width: int | None = None
    screenshot_height: int | None = None
    images: list[ImageRef] = Field(default_factory=list)
    open_tabs: list[str] = Field(default_factory=list)
    downloads: list[str] = Field(default_factory=list)
    dom_version: str | None = None


# ---------------------------------------------------------------------------
# Actions, policy and approvals
# ---------------------------------------------------------------------------


class ExpectedResult(_C):
    url_contains: str | None = None
    text_visible: str | None = None
    element_value: dict[str, str] | None = None
    download_name: str | None = None
    new_tab: bool | None = None


class ActionIntent(_C):
    action_id: str
    task_id: str
    worker_id: str | None = None
    observation_id: str | None = None
    operation: str
    target: dict[str, Any] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected: ExpectedResult | None = None
    idempotency_key: str
    rationale: str | None = None


class PolicyDecision(_C):
    verdict: PolicyVerdict
    category: ActionCategory
    reason: str
    policy_version: str
    destination: str | None = None
    consequence: str | None = None
    standing_rule_id: str | None = None
    error_code: ErrorCode | None = None


class ApprovalRequest(_C):
    approval_id: str
    task_id: str
    action_id: str
    owner: str
    operation: str
    action_digest: str
    destination: str | None
    consequence: str
    preview: str
    preconditions: dict[str, Any] = Field(default_factory=dict)
    policy_version: str
    expires_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING


class ApprovalDecision(_C):
    decision: Literal["approve", "deny"]
    note: str | None = None


# ---------------------------------------------------------------------------
# Receipts, evidence, artifacts, worker jobs
# ---------------------------------------------------------------------------


class ArtifactManifest(_C):
    artifact_id: str
    task_id: str | None
    name: str
    rel_path: str = Field(description="Path relative to the workspace root")
    media_type: str
    bytes: int
    sha256: str
    kind: str = Field(description="report, evidence, observation, media, input, transfer")
    parent_artifact_id: str | None = None
    crop: Rect | None = None
    width: int | None = None
    height: int | None = None
    resize_scale: float | None = None
    source_map: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ReceiptError(_C):
    code: ErrorCode
    message: str
    retryable: bool = False


class ActionReceipt(_C):
    action_id: str
    job_id: str | None = None
    worker_id: str | None = None
    fencing_token: int | None = None
    status: ReceiptStatus
    started_at: str
    finished_at: str
    result: dict[str, Any] = Field(default_factory=dict)
    observation: Observation | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    error: ReceiptError | None = None
    component_versions: dict[str, str] = Field(default_factory=dict)


class VideoInterval(_C):
    start_seconds: float
    end_seconds: float
    frame_artifact_ids: list[str] = Field(default_factory=list)
    transcript_segment_ids: list[str] = Field(default_factory=list)


class EvidenceRecord(_C):
    evidence_id: str
    task_id: str
    kind: Literal["text_excerpt", "image_region", "video_interval", "page_capture", "search_result"]
    source_url: str
    final_url: str | None = None
    source_date: str | None = None
    captured_at: str
    excerpt: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict, description="Character offsets, CSS path or text fragment")
    image_artifact_id: str | None = None
    region: Rect | None = None
    video_interval: VideoInterval | None = None
    content_sha256: str
    limitations: str | None = None
    provider: str | None = None
    claim_ids: list[str] = Field(default_factory=list)


class Claim(_C):
    claim_id: str
    task_id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    support: Literal["supported", "unsupported", "disputed", "uncertain"] = "supported"
    review_note: str | None = None


class WorkerJob(_C):
    job_id: str
    task_id: str
    action_id: str
    worker_id: str
    lease_id: str
    resource: str
    fencing_token: int
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected: ExpectedResult | None = None
    idempotency_key: str
    deadline: str
    lease_expires_at: str
    issued_at: str
    approval_id: str | None = None
    attachment_id: str | None = Field(default=None, description="Authorized browser attachment to act in")
    declared_inputs: list[str] = Field(default_factory=list)
    declared_outputs: list[str] = Field(default_factory=list)


class WorkerRegistration(_C):
    worker_id: str
    capabilities: list[str]
    session_epoch: str = Field(description="Unique per worker process start; newer epochs fence out stale processes")
    version: str
    interactive_resources: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    platform: str | None = None


class WorkerHeartbeat(_C):
    session_epoch: str
    busy_job_id: str | None = None
    health: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Search and retrieval (provider-independent)
# ---------------------------------------------------------------------------


class SearchResult(_C):
    provider: str
    rank: int
    title: str
    url: str = Field(description="Canonical URL")
    snippet: str = ""
    retrieved_at: str
    source_date: str | None = None


class RetrievedPage(_C):
    provider: str
    source_url: str
    final_url: str
    status: int
    captured_at: str
    title: str = ""
    text: str = ""
    html: str | None = None
    source_date: str | None = None
    content_sha256: str
    screenshot_artifact_id: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class BudgetStatus(_C):
    currency: str
    period: str
    period_timezone: str
    ceiling: float
    settled: float
    reserved: float
    unknown: float
    remaining: float
    utilisation: float
    thresholds_crossed: list[float]
    per_task_ceiling: float
    hosted_routes_enabled: list[str]
    disabled_routes: dict[str, str]


TaskView.model_rebuild()

CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "TaskRequest": TaskRequest,
    "TaskView": TaskView,
    "Observation": Observation,
    "ActionIntent": ActionIntent,
    "PolicyDecision": PolicyDecision,
    "ApprovalRequest": ApprovalRequest,
    "ApprovalDecision": ApprovalDecision,
    "ActionReceipt": ActionReceipt,
    "EvidenceRecord": EvidenceRecord,
    "Claim": Claim,
    "ArtifactManifest": ArtifactManifest,
    "WorkerJob": WorkerJob,
    "WorkerRegistration": WorkerRegistration,
    "WorkerHeartbeat": WorkerHeartbeat,
    "SearchResult": SearchResult,
    "RetrievedPage": RetrievedPage,
    "BudgetStatus": BudgetStatus,
}
