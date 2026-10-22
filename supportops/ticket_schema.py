"""v4 附加工单表；建议复用已有私有问答，不保存第二份模型正文。"""

TICKET_TABLES = {"tickets", "ticket_suggestions", "ticket_events"}
TICKET_SCHEMA = (
    """CREATE TABLE tickets (
        workspace_id TEXT NOT NULL, id TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id),
        title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL, priority TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','in_progress','resolved')), revision INTEGER NOT NULL DEFAULT 0,
        accepted_suggestion_id TEXT, create_key TEXT NOT NULL, request_sha TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,owner_id), UNIQUE(workspace_id,owner_id,create_key),
        FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY(workspace_id,id,accepted_suggestion_id) REFERENCES ticket_suggestions(workspace_id,ticket_id,id)
          DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE ticket_suggestions (
        workspace_id TEXT NOT NULL, ticket_id TEXT NOT NULL, id TEXT NOT NULL, owner_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, answer_id TEXT, run_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('generating','ready','failed')), error_code TEXT,
        idempotency_key TEXT NOT NULL, request_sha TEXT NOT NULL, expires_at REAL NOT NULL,
        created_at TEXT NOT NULL, finished_at TEXT,
        PRIMARY KEY(workspace_id,ticket_id,id), UNIQUE(workspace_id,ticket_id,idempotency_key),
        FOREIGN KEY(workspace_id,ticket_id,owner_id) REFERENCES tickets(workspace_id,id,owner_id),
        FOREIGN KEY(workspace_id,conversation_id,owner_id) REFERENCES conversations(workspace_id,id,owner_id),
        FOREIGN KEY(workspace_id,answer_id,conversation_id) REFERENCES answers(workspace_id,id,conversation_id),
        FOREIGN KEY(workspace_id,run_id) REFERENCES answer_runs(workspace_id,id))""",
    """CREATE UNIQUE INDEX one_generating_ticket_suggestion ON ticket_suggestions(workspace_id,ticket_id)
        WHERE state='generating'""",
    """CREATE TABLE ticket_events (
        workspace_id TEXT NOT NULL, ticket_id TEXT NOT NULL, id TEXT NOT NULL,
        actor_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL, content TEXT NOT NULL,
        idempotency_key TEXT, request_sha TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,ticket_id,id), UNIQUE(workspace_id,ticket_id,idempotency_key),
        FOREIGN KEY(workspace_id,ticket_id) REFERENCES tickets(workspace_id,id))""",
)
