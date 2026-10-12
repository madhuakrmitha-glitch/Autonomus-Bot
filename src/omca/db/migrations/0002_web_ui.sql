-- Local web UI: the owner's conversation as shown in the browser.
-- Assistant rows mirror outbox messages (one row per outbox text/document, edits update it in place);
-- owner rows record what was typed in the UI. `rev` grows on every insert or edit so the UI can poll
-- for changes since the last revision it saw.
CREATE TABLE web_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id    TEXT UNIQUE,
    task_id      TEXT,
    role         TEXT NOT NULL,           -- owner, assistant
    kind         TEXT NOT NULL,           -- text, document
    text         TEXT NOT NULL,
    buttons_json TEXT,
    filename     TEXT,
    rev          INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_web_messages_rev ON web_messages(rev);

-- Outbox rows already applied to web_messages (edits are applied once, in outbox order).
CREATE TABLE web_mirrored (
    outbox_id  TEXT PRIMARY KEY
);
