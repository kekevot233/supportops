"""v3 附加迁移：会话、整轮消息、回答引用、反馈与脱敏运行记录。"""

CHAT_TABLES = {"conversations", "answer_runs", "answers", "answer_citations", "chat_messages", "answer_feedback"}
CHAT_SCHEMA = (
    """CREATE TABLE conversations (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id), id TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,id,owner_id))""",
    """CREATE TABLE answer_runs (
        workspace_id TEXT NOT NULL, id TEXT NOT NULL, conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, request_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed')), error_code TEXT,
        expires_at REAL NOT NULL, model_mode TEXT NOT NULL, model_attempts INTEGER NOT NULL DEFAULT 0,
        estimated_input_tokens INTEGER, estimated_output_tokens INTEGER,
        retrieval_ms REAL NOT NULL DEFAULT 0, generation_ms REAL NOT NULL DEFAULT 0, total_ms REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, finished_at TEXT, PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,conversation_id,idempotency_key),
        FOREIGN KEY(workspace_id,conversation_id,owner_id) REFERENCES conversations(workspace_id,id,owner_id))""",
    """CREATE UNIQUE INDEX one_running_answer ON answer_runs(workspace_id,conversation_id) WHERE state='running'""",
    """CREATE TABLE answers (
        workspace_id TEXT NOT NULL, id TEXT NOT NULL, conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        run_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('answered','insufficient_evidence')),
        content TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,id),
        UNIQUE(workspace_id,run_id), UNIQUE(workspace_id,id,conversation_id),
        FOREIGN KEY(workspace_id,conversation_id,owner_id) REFERENCES conversations(workspace_id,id,owner_id),
        FOREIGN KEY(workspace_id,run_id) REFERENCES answer_runs(workspace_id,id))""",
    """CREATE TABLE answer_citations (
        workspace_id TEXT NOT NULL, answer_id TEXT NOT NULL, label TEXT NOT NULL,
        document_id TEXT NOT NULL, version_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
        PRIMARY KEY(workspace_id,answer_id,label),
        FOREIGN KEY(workspace_id,answer_id) REFERENCES answers(workspace_id,id),
        FOREIGN KEY(workspace_id,document_id,version_id) REFERENCES document_versions(workspace_id,document_id,id))""",
    """CREATE TABLE chat_messages (
        workspace_id TEXT NOT NULL, conversation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        answer_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
        PRIMARY KEY(workspace_id,conversation_id,sequence),
        FOREIGN KEY(workspace_id,conversation_id) REFERENCES conversations(workspace_id,id),
        FOREIGN KEY(workspace_id,answer_id,conversation_id) REFERENCES answers(workspace_id,id,conversation_id))""",
    """CREATE TABLE answer_feedback (
        workspace_id TEXT NOT NULL, answer_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
        rating TEXT NOT NULL CHECK(rating IN ('useful','not_useful')), reason TEXT, comment TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY(workspace_id,answer_id,user_id),
        FOREIGN KEY(workspace_id,answer_id) REFERENCES answers(workspace_id,id))""",
)
