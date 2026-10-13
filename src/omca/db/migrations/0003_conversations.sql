-- Each web chat is its own conversation: tasks and messages carry the conversation they belong to.
-- NULL means "not part of a conversation" (older rows, Telegram), and such a task is its own thread.
ALTER TABLE tasks ADD COLUMN conversation_id TEXT;
ALTER TABLE web_messages ADD COLUMN conversation_id TEXT;
CREATE INDEX idx_tasks_conversation ON tasks(conversation_id, created_at);
CREATE INDEX idx_web_messages_conversation ON web_messages(conversation_id);
