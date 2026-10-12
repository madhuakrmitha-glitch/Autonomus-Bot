-- Open Model Computer Assistant: initial schema (spec section 16).
-- Single coordinator writer. Machine timestamps are ISO-8601 UTC strings.

CREATE TABLE tasks (
    id                  TEXT PRIMARY KEY,
    owner               TEXT NOT NULL,
    goal                TEXT NOT NULL,
    workflow            TEXT NOT NULL,
    request_json        TEXT NOT NULL,
    status              TEXT NOT NULL,
    phase               TEXT,
    plan_json           TEXT,
    current_step        TEXT,
    worker_id           TEXT,
    limits_json         TEXT NOT NULL,
    blocking_reason     TEXT,
    error_code          TEXT,
    result_json         TEXT,
    checkpoint_version  INTEGER NOT NULL DEFAULT 0,
    action_count        INTEGER NOT NULL DEFAULT 0,
    active_ms           INTEGER NOT NULL DEFAULT 0,
    origin              TEXT NOT NULL DEFAULT 'owner',
    schedule_run_id     TEXT,
    dedup_key           TEXT UNIQUE,
    status_message_ref  TEXT,
    last_progress_at    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    finished_at         TEXT
);
CREATE INDEX idx_tasks_status ON tasks(status);

CREATE TABLE task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    data_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_task_events_task ON task_events(task_id, id);

-- Durable step journal: completed steps are replayed, never re-executed.
CREATE TABLE task_steps (
    task_id      TEXT NOT NULL,
    step_key     TEXT NOT NULL,
    status       TEXT NOT NULL,           -- STARTED, DONE, FAILED
    attempt      INTEGER NOT NULL DEFAULT 1,
    output_json  TEXT,
    error_json   TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    PRIMARY KEY (task_id, step_key)
);

CREATE TABLE task_messages (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,           -- steer, owner_input
    text         TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE actions (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    step_key         TEXT,
    worker_id        TEXT,
    observation_id   TEXT,
    operation        TEXT NOT NULL,
    target_json      TEXT NOT NULL,
    arguments_json   TEXT NOT NULL,
    arguments_hash   TEXT NOT NULL,
    expected_json    TEXT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    category         TEXT,
    verdict          TEXT,
    policy_reason    TEXT,
    policy_version   TEXT,
    destination      TEXT,
    approval_id      TEXT,
    consequential    INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 0,
    receipt_json     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_actions_task ON actions(task_id);
CREATE INDEX idx_actions_status ON actions(status);

CREATE TABLE approvals (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    action_id        TEXT NOT NULL,
    owner            TEXT NOT NULL,
    operation        TEXT NOT NULL,
    action_digest    TEXT NOT NULL,
    destination      TEXT,
    consequence      TEXT NOT NULL,
    preview          TEXT NOT NULL,
    preconditions_json TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    nonce            TEXT NOT NULL,
    signature        TEXT NOT NULL,
    status           TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    decided_at       TEXT,
    decided_by       TEXT,
    decision_note    TEXT,
    consumed_at      TEXT,
    message_ref      TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_approvals_task ON approvals(task_id);

CREATE TABLE observations (
    id                     TEXT PRIMARY KEY,
    task_id                TEXT,
    worker_id              TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    url                    TEXT NOT NULL,
    title                  TEXT,
    captured_at            TEXT NOT NULL,
    viewport_json          TEXT NOT NULL,
    screenshot_artifact_id TEXT,
    screenshot_sha256      TEXT,
    elements_json          TEXT NOT NULL,
    data_json              TEXT NOT NULL
);
CREATE INDEX idx_observations_task ON observations(task_id);

CREATE TABLE evidence (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    kind               TEXT NOT NULL,
    source_url         TEXT NOT NULL,
    final_url          TEXT,
    source_date        TEXT,
    captured_at        TEXT NOT NULL,
    excerpt            TEXT,
    locator_json       TEXT NOT NULL DEFAULT '{}',
    image_artifact_id  TEXT,
    region_json        TEXT,
    video_interval_json TEXT,
    content_sha256     TEXT NOT NULL,
    limitations        TEXT,
    provider           TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX idx_evidence_task ON evidence(task_id);

CREATE TABLE claims (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    text          TEXT NOT NULL,
    support       TEXT NOT NULL,
    review_note   TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE claim_evidence (
    claim_id     TEXT NOT NULL,
    evidence_id  TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_id)
);

CREATE TABLE artifacts (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT,
    name                TEXT NOT NULL,
    rel_path            TEXT NOT NULL UNIQUE,
    media_type          TEXT NOT NULL,
    bytes               INTEGER NOT NULL,
    sha256              TEXT NOT NULL,
    kind                TEXT NOT NULL,
    parent_artifact_id  TEXT,
    crop_json           TEXT,
    width               INTEGER,
    height              INTEGER,
    resize_scale        REAL,
    source_map_json     TEXT NOT NULL DEFAULT '{}',
    validation_json     TEXT NOT NULL DEFAULT '{}',
    retention_class     TEXT NOT NULL,
    expires_at          TEXT,
    deleted_at          TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_artifacts_task ON artifacts(task_id);

CREATE TABLE model_calls (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT,
    route_id           TEXT NOT NULL,
    purpose            TEXT NOT NULL,
    caller             TEXT NOT NULL,
    period             TEXT NOT NULL,
    status             TEXT NOT NULL,      -- RESERVED, SETTLED, RELEASED, UNKNOWN, REFUSED
    est_input_tokens   INTEGER NOT NULL DEFAULT 0,
    est_images         INTEGER NOT NULL DEFAULT 0,
    max_output_tokens  INTEGER NOT NULL DEFAULT 0,
    reservation        REAL NOT NULL DEFAULT 0,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    image_tokens       INTEGER,
    cost               REAL,
    currency           TEXT NOT NULL,
    latency_ms         INTEGER,
    attempt            INTEGER NOT NULL DEFAULT 1,
    usage_source       TEXT,
    error              TEXT,
    created_at         TEXT NOT NULL,
    settled_at         TEXT
);
CREATE INDEX idx_model_calls_period ON model_calls(period, status);
CREATE INDEX idx_model_calls_task ON model_calls(task_id);

CREATE TABLE endpoint_sessions (
    id             TEXT PRIMARY KEY,
    route_id       TEXT NOT NULL,
    period         TEXT NOT NULL,
    status         TEXT NOT NULL,          -- RESERVED, RUNNING, SETTLED, UNKNOWN
    reservation    REAL NOT NULL,
    cost           REAL,
    currency       TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    approved_until TEXT NOT NULL,
    stopped_at     TEXT
);

CREATE TABLE budget_notices (
    period     TEXT NOT NULL,
    threshold  REAL NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (period, threshold)
);

CREATE TABLE route_probes (
    route_id       TEXT NOT NULL,
    route_digest   TEXT NOT NULL,
    probe          TEXT NOT NULL,
    passed         INTEGER NOT NULL,
    detail_json    TEXT NOT NULL,
    ran_at         TEXT NOT NULL,
    PRIMARY KEY (route_id, route_digest, probe)
);

CREATE TABLE workers (
    id               TEXT PRIMARY KEY,
    capabilities_json TEXT NOT NULL,
    session_epoch    TEXT,
    version          TEXT,
    platform         TEXT,
    resources_json   TEXT NOT NULL DEFAULT '[]',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL,       -- ONLINE, OFFLINE
    health_json      TEXT NOT NULL DEFAULT '{}',
    last_heartbeat   TEXT,
    registered_at    TEXT NOT NULL
);

CREATE TABLE resources (
    name             TEXT PRIMARY KEY,
    fencing_counter  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE leases (
    id             TEXT PRIMARY KEY,
    resource       TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    worker_id      TEXT NOT NULL,
    fencing_token  INTEGER NOT NULL,
    status         TEXT NOT NULL,         -- ACTIVE, RELEASED, EXPIRED
    acquired_at    TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    released_at    TEXT
);
CREATE INDEX idx_leases_resource ON leases(resource, status);

CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    action_id       TEXT NOT NULL UNIQUE,
    worker_id       TEXT NOT NULL,
    lease_id        TEXT NOT NULL,
    fencing_token   INTEGER NOT NULL,
    operation       TEXT NOT NULL,
    job_json        TEXT NOT NULL,
    status          TEXT NOT NULL,        -- PENDING, CLAIMED, DONE, EXPIRED, CANCELLED
    claimed_epoch   TEXT,
    claimed_at      TEXT,
    receipt_json    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_jobs_worker ON jobs(worker_id, status);

CREATE TABLE schedules (
    id               TEXT PRIMARY KEY,
    config_json      TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    paused           INTEGER NOT NULL DEFAULT 0,
    last_fired_at    TEXT,
    last_planned_at  TEXT,
    last_completed_run_id TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE schedule_runs (
    id             TEXT PRIMARY KEY,
    schedule_id    TEXT NOT NULL,
    planned_at     TEXT NOT NULL,
    dedup_key      TEXT NOT NULL UNIQUE,
    task_id        TEXT,
    status         TEXT NOT NULL,         -- CREATED, SKIPPED_OVERLAP, MISSED, COMPLETED, FAILED
    note           TEXT,
    created_at     TEXT NOT NULL
);

-- Stable memory (FR13), kept apart from temporary observations.
CREATE TABLE preferences (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE procedures (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    steps_json  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);
CREATE TABLE freshness_rules (
    id           TEXT PRIMARY KEY,
    pattern      TEXT NOT NULL,
    max_age_days REAL NOT NULL,
    note         TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE policy_versions (
    version      TEXT PRIMARY KEY,
    content_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE standing_rule_uses (
    rule_id     TEXT NOT NULL,
    action_id   TEXT NOT NULL,
    used_at     TEXT NOT NULL,
    PRIMARY KEY (rule_id, action_id)
);

-- Channel plumbing.
CREATE TABLE inbound_updates (
    channel     TEXT NOT NULL,
    update_id   TEXT NOT NULL,
    sender      TEXT,
    accepted    INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (channel, update_id)
);
CREATE TABLE outbox (
    id           TEXT PRIMARY KEY,
    task_id      TEXT,
    kind         TEXT NOT NULL,           -- text, document, approval, edit
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL,           -- PENDING, SENT, FAILED
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    result_json  TEXT,
    created_at   TEXT NOT NULL,
    sent_at      TEXT
);
CREATE INDEX idx_outbox_status ON outbox(status, created_at);

CREATE TABLE kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
