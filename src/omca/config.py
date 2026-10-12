"""Application configuration.

``config/application.example.yaml`` is the normative default inventory. This module
validates it, resolves relative paths against the config file's directory and refuses
literal secrets. Nothing here activates a hosted model route: a route is usable only
when it is enabled, priced (or local), its FX rate is known and its probes passed.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .secrets import validate_ref


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_tz(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {value!r}") from exc
    return value


class OwnerConfig(_Base):
    name: str = "owner"
    telegram_user_ids: list[int] = Field(default_factory=list, description="Owner allowlist (numeric Telegram user IDs)")
    approver_user_ids: list[int] | None = Field(
        default=None, description="Identities allowed to decide approvals; defaults to the owner allowlist"
    )
    display_timezone: str = "Asia/Kolkata"

    _tz = field_validator("display_timezone")(_check_tz)

    @property
    def approvers(self) -> list[int]:
        return self.approver_user_ids if self.approver_user_ids is not None else self.telegram_user_ids


class OpenClawBridgeConfig(_Base):
    bridge_token_ref: str | None = None
    outbound_url: str | None = Field(
        default=None, description="Plugin HTTP route used for outbound delivery, e.g. http://127.0.0.1:18789/omca/outbound"
    )

    @field_validator("bridge_token_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return validate_ref(v, "telegram.openclaw.bridge_token_ref")


class TelegramConfig(_Base):
    mode: Literal["openclaw", "direct", "disabled"] = "openclaw"
    bot_token_ref: str | None = None
    api_base: str = "https://api.telegram.org"
    poll_timeout_seconds: int = 30
    pairing_code_ttl_seconds: int = 600
    openclaw: OpenClawBridgeConfig = Field(default_factory=OpenClawBridgeConfig)

    @field_validator("bot_token_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return validate_ref(v, "telegram.bot_token_ref")


class ServerConfig(_Base):
    host: str = "127.0.0.1"
    port: int = 8765
    public_base_url: str | None = None
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    owner_api_token_ref: str | None = None
    model_gateway_token_ref: str | None = None
    approval_signing_key_ref: str | None = None

    @field_validator("owner_api_token_ref", "model_gateway_token_ref", "approval_signing_key_ref")
    @classmethod
    def _ref(cls, v: str | None, info: Any) -> str | None:
        return validate_ref(v, f"server.{info.field_name}")


class StorageConfig(_Base):
    data_dir: Path = Path("./data")
    workspace_root: Path = Path("./workspace")
    reports_dir: Path | None = Field(default=None, description="Owner-visible report location; defaults to <workspace_root>/reports")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "omca.sqlite3"

    @property
    def owner_reports_dir(self) -> Path:
        return self.reports_dir or (self.workspace_root / "reports")


class RetentionConfig(_Base):
    observation_days: int = 7
    media_days: int = 7
    action_log_days: int = 90
    usage_ledger_months: int = 12


class FxRate(_Base):
    rate: float | None = None
    source: str | None = None
    effective_date: date | None = None


class BudgetConfig(_Base):
    currency: str = "INR"
    monthly_ceiling: float = 2000.0
    period_timezone: str = "Asia/Kolkata"
    per_task_reservation_ceiling: float = 50.0
    warning_thresholds: list[float] = Field(default_factory=lambda: [0.7, 0.9])
    reserve_margin: float = Field(default=0.25, description="Extra fraction added to every reservation as a conservative buffer")
    fx: dict[str, FxRate] = Field(default_factory=dict, description="Rates converting a provider currency to the budget currency")

    _tz = field_validator("period_timezone")(_check_tz)


class LimitsConfig(_Base):
    max_actions_per_task: int = 40
    max_retries_per_action: int = 2
    max_task_active_minutes: float = 20
    approval_expiry_minutes: float = 15
    observation_max_age_seconds: float = 30
    progress_interval_seconds: float = 60
    worker_heartbeat_seconds: float = 15
    lease_ttl_seconds: float = 60
    max_concurrent_tasks: int = 4
    owner_input_timeout_hours: float = 24


class ModelPricing(_Base):
    currency: str = "USD"
    input_per_million: float | None = None
    output_per_million: float | None = None
    image_per_unit: float | None = None
    per_hour: float | None = Field(default=None, description="Time-billed dedicated endpoints")
    source: str | None = None
    effective_date: date | None = None


class DedicatedEndpointConfig(_Base):
    controller: Literal["huggingface", "manual"] = "huggingface"
    namespace: str | None = None
    name: str | None = None
    api_base: str = "https://api.endpoints.huggingface.cloud/v2"
    api_token_ref: str | None = None
    max_session_hours: float = 1.0
    initialization_minutes: float = 10.0

    @field_validator("api_token_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return validate_ref(v, "dedicated.api_token_ref")


Modality = Literal["text", "image", "video", "audio"]


class ModelRoute(_Base):
    id: str
    provider: Literal["openai_compatible", "ollama", "hf_inference_providers", "hf_dedicated_endpoint", "fixture"]
    base_url: str | None = None
    model: str
    revision: str | None = None
    license: str = Field(description="License identifier of the open weights, e.g. apache-2.0")
    license_url: str | None = None
    weights_url: str | None = None
    open_weights: bool = Field(description="Downloadable weights with a recorded license permitting the intended use")
    modalities: list[Modality] = Field(default_factory=lambda: ["text"])
    supports_tools: bool = True
    supports_streaming: bool = True
    native_video: bool = False
    context_limit: int = 32768
    max_output_tokens: int = 4096
    coordinate_system: Literal["normalized_1000", "pixels", "normalized_1"] = "normalized_1000"
    max_image_side: int = 1280
    image_patch_px: int = 28
    billing: Literal["tokens", "time", "none"] = "tokens"
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    auth_ref: str | None = None
    enabled: bool = False
    request_timeout_seconds: float = 180
    extra_body: dict[str, Any] = Field(default_factory=dict)
    usage_reporting: bool = True
    dedicated: DedicatedEndpointConfig | None = None
    fixture: str | None = Field(default=None, description="Fixture behaviour name (tests only)")

    @field_validator("auth_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return validate_ref(v, "models.routes[].auth_ref")

    @model_validator(mode="after")
    def _check(self) -> ModelRoute:
        if self.provider != "fixture" and not self.base_url:
            raise ValueError(f"route {self.id}: base_url is required")
        if self.billing == "time" and self.dedicated is None:
            raise ValueError(f"route {self.id}: time-billed routes need a dedicated endpoint controller")
        return self

    def supports(self, modality: str) -> bool:
        return modality in self.modalities


RoleName = Literal["planning", "vision", "summarisation", "review", "embeddings"]


class ModelRoles(_Base):
    planning: str | None = None
    vision: str | None = None
    summarisation: str | None = None
    review: str | None = None
    embeddings: str | None = None
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class SpeechConfig(_Base):
    engine: Literal["faster-whisper", "disabled"] = "faster-whisper"
    model: str = "small"
    model_revision: str | None = None
    license: str = "mit"
    weights_url: str = "https://huggingface.co/Systran/faster-whisper-small"
    device: str = "auto"
    compute_type: str = "int8"


class ModelsConfig(_Base):
    require_probes: bool = True
    allow_fixture_routes: bool = False
    roles: ModelRoles = Field(default_factory=ModelRoles)
    routes: list[ModelRoute] = Field(default_factory=list)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)

    @model_validator(mode="after")
    def _check(self) -> ModelsConfig:
        ids = [r.id for r in self.routes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate model route ids")
        known = set(ids)
        for role, rid in self.roles.model_dump(exclude={"fallbacks"}).items():
            if rid and rid not in known:
                raise ValueError(f"role {role} refers to unknown route {rid}")
        for role, chain in self.roles.fallbacks.items():
            for rid in chain:
                if rid not in known:
                    raise ValueError(f"fallback for {role} refers to unknown route {rid}")
        for r in self.routes:
            if r.provider == "fixture" and not self.allow_fixture_routes:
                raise ValueError(f"route {r.id}: fixture routes are only allowed in test configurations")
        return self

    def route(self, route_id: str) -> ModelRoute | None:
        return next((r for r in self.routes if r.id == route_id), None)


class FirecrawlConfig(_Base):
    base_url: str = "https://api.firecrawl.dev"
    api_key_ref: str | None = None
    api_version: Literal["v1", "v2"] = "v2"
    timeout_seconds: float = 60

    @field_validator("api_key_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return validate_ref(v, "search.firecrawl.api_key_ref")


class SearxngConfig(_Base):
    base_url: str | None = None
    timeout_seconds: float = 30


class HttpRetrievalConfig(_Base):
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OMCA-research/0.1"
    timeout_seconds: float = 30
    max_bytes: int = 5_000_000


class SearchConfig(_Base):
    adapter: str = "firecrawl"
    retrieval_adapter: str = "firecrawl"
    firecrawl: FirecrawlConfig = Field(default_factory=FirecrawlConfig)
    searxng: SearxngConfig = Field(default_factory=SearxngConfig)
    http: HttpRetrievalConfig = Field(default_factory=HttpRetrievalConfig)
    max_results_per_query: int = 8
    max_queries_per_task: int = 4
    max_pages_per_task: int = 6
    prefer_domains: list[str] = Field(default_factory=list)
    demote_domains: list[str] = Field(default_factory=list)
    block_domains: list[str] = Field(default_factory=list)
    allowed_private_hosts: list[str] = Field(default_factory=list, description="Fixture or intranet hosts retrieval may reach")


class BrowserAttachment(_Base):
    id: str
    worker_id: str
    mode: Literal["cdp", "persistent_profile", "launch"] = "persistent_profile"
    cdp_url: str | None = None
    user_data_dir: str | None = None
    channel: Literal["msedge", "chrome", "chromium"] = "msedge"
    headless: bool = False
    authorized: bool = Field(default=False, description="Explicit owner authorization to use this session")
    resource: str | None = Field(default=None, description="Lease resource; defaults to <worker_id>:ui")
    allowed_private_origins: list[str] = Field(default_factory=list)
    viewport: tuple[int, int] | None = None
    device_scale_factor: float | None = Field(default=None, description="launch mode only (fixtures, HiDPI tests)")

    @property
    def lease_resource(self) -> str:
        return self.resource or f"{self.worker_id}:ui"

    @field_validator("user_data_dir")
    @classmethod
    def _expand(cls, v: str | None) -> str | None:
        return os.path.expanduser(os.path.expandvars(v)) if v else v

    @model_validator(mode="after")
    def _check(self) -> BrowserAttachment:
        if self.mode == "cdp":
            if not self.cdp_url:
                raise ValueError(f"browser attachment {self.id}: cdp mode needs cdp_url")
            host = re.sub(r"^\w+://", "", self.cdp_url).split(":")[0].split("/")[0]
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError(f"browser attachment {self.id}: debugging port must stay on loopback")
        return self


class BrowserConfig(_Base):
    attachments: list[BrowserAttachment] = Field(default_factory=list)
    screenshot_max_side_for_model: int = 1280
    max_page_text_chars: int = 20000


WorkerCapability = Literal["browser", "video", "document", "desktop", "spreadsheet", "email", "calendar"]


class WorkerEntry(_Base):
    id: str
    display_name: str | None = None
    credential_ref: str | None = None
    capabilities: list[WorkerCapability] = Field(default_factory=lambda: ["browser", "video", "document"])
    enabled: bool = True

    @field_validator("credential_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return validate_ref(v, "workers[].credential_ref")

    @property
    def ui_resource(self) -> str:
        return f"{self.id}:ui"


class StandingRule(_Base):
    id: str
    description: str
    operation: str
    origin: str | None = None
    recipient: str | None = None
    path_prefix: str | None = None
    element_name_pattern: str | None = None
    expires_at: datetime | None = None
    max_uses: int | None = None

    @model_validator(mode="after")
    def _narrow(self) -> StandingRule:
        if not any([self.origin, self.recipient, self.path_prefix, self.element_name_pattern]):
            raise ValueError(f"standing rule {self.id} must be narrowly scoped (origin, recipient, path or element)")
        return self


class Contact(_Base):
    name: str
    kind: Literal["email", "telegram"]
    address: str


class PolicyConfig(_Base):
    standing_rules: list[StandingRule] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    download_allowed_extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".csv", ".txt", ".json",
                                 ".xlsx", ".docx", ".pptx", ".mp4", ".webm", ".mp3", ".wav", ".vtt", ".srt"]
    )
    max_download_bytes: int = 500 * 1024 * 1024
    extra_consequential_terms: list[str] = Field(default_factory=list)


class ScheduleConfig(_Base):
    id: str
    name: str
    cron: str
    timezone: str = "Asia/Kolkata"
    workflow: Literal["research_report", "video_analysis", "browser_task"] = "research_report"
    goal_template: str
    sources: list[str] = Field(default_factory=list)
    output_destination: Literal["telegram_owner"] = "telegram_owner"
    spending_limit: float = 50.0
    concurrency: Literal["forbid", "allow"] = "forbid"
    missed_run_policy: Literal["skip", "run_latest"] = "skip"
    escalation: Literal["notify_owner"] = "notify_owner"
    enabled: bool = True

    _tz = field_validator("timezone")(_check_tz)


class VideoConfig(_Base):
    max_duration_seconds: float = 3600
    segment_seconds: float = 600
    max_coarse_frames: int = 48
    max_dense_frames_per_interval: int = 12
    max_total_frames: int = 160
    frame_max_side: int = 1024
    frames_per_model_call: int = 8
    scene_threshold: float = 0.25
    live_max_seconds: float = 300
    download_max_height: int = 720
    download_max_filesize_mb: int = 500


class ReportsConfig(_Base):
    max_excerpt_chars: int = 300
    pdf_companion: bool = False


class ApplicationConfig(_Base):
    version: Literal[1] = 1
    testing: bool = False
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    workers: list[WorkerEntry] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    schedules: list[ScheduleConfig] = Field(default_factory=list)
    video: VideoConfig = Field(default_factory=VideoConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)

    @model_validator(mode="after")
    def _check(self) -> ApplicationConfig:
        worker_ids = {w.id for w in self.workers}
        for att in self.browser.attachments:
            if att.worker_id not in worker_ids:
                raise ValueError(f"browser attachment {att.id} refers to unknown worker {att.worker_id}")
        return self

    def worker(self, worker_id: str) -> WorkerEntry | None:
        return next((w for w in self.workers if w.id == worker_id), None)

    def resolve_paths(self, base: Path) -> ApplicationConfig:
        def fix(p: Path | None) -> Path | None:
            if p is None:
                return None
            p = Path(os.path.expandvars(str(p))).expanduser()
            return p if p.is_absolute() else (base / p).resolve()

        self.storage.data_dir = fix(self.storage.data_dir)  # type: ignore[assignment]
        self.storage.workspace_root = fix(self.storage.workspace_root)  # type: ignore[assignment]
        self.storage.reports_dir = fix(self.storage.reports_dir)
        return self


def load_config(path: str | Path) -> ApplicationConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = ApplicationConfig.model_validate(data)
    return cfg.resolve_paths(path.parent.resolve())


def config_from_dict(data: dict[str, Any], base: Path | None = None) -> ApplicationConfig:
    cfg = ApplicationConfig.model_validate(data)
    return cfg.resolve_paths(base or Path.cwd())


# ---------------------------------------------------------------------------
# Worker-side configuration (runs inside each Windows VM's interactive session)
# ---------------------------------------------------------------------------


class SmtpConfig(_Base):
    host: str
    port: int = 587
    starttls: bool = True
    username_ref: str | None = None
    password_ref: str | None = None
    from_address: str

    @field_validator("username_ref", "password_ref")
    @classmethod
    def _ref(cls, v: str | None, info: Any) -> str | None:
        return validate_ref(v, f"smtp.{info.field_name}")


class WorkerProcessConfig(_Base):
    worker_id: str
    coordinator_url: str = "http://127.0.0.1:8765"
    credential_ref: str
    ca_file: str | None = None
    workspace_dir: Path = Path("./worker-workspace")
    capabilities: list[WorkerCapability] = Field(default_factory=lambda: ["browser", "video", "document"])
    attachments: list[BrowserAttachment] = Field(default_factory=list)
    claim_wait_seconds: float = 20
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    smtp: SmtpConfig | None = None
    allowed_download_extensions: list[str] = Field(default_factory=lambda: PolicyConfig().download_allowed_extensions)

    @field_validator("credential_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        out = validate_ref(v, "credential_ref")
        if not out:
            raise ValueError("credential_ref is required")
        return out


def load_worker_config(path: str | Path) -> WorkerProcessConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = WorkerProcessConfig.model_validate(data)
    cfg.workspace_dir = Path(os.path.expandvars(str(cfg.workspace_dir))).expanduser()
    if not cfg.workspace_dir.is_absolute():
        cfg.workspace_dir = (path.parent / cfg.workspace_dir).resolve()
    return cfg
