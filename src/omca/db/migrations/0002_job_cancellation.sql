-- A claimed read-only job can be interrupted: the worker learns about it from its heartbeat.
ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
