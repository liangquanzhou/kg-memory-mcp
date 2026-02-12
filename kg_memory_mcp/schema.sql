-- kg-memory-mcp schema
-- 知识图谱 + 对话存档，部署到 knowledge_base 数据库
-- 依赖: pgvector 扩展（已安装）

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- 知识图谱层 (kg_ 前缀)
-- ============================================================

CREATE TABLE IF NOT EXISTS kg_entities (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    entity_type TEXT NOT NULL,
    description TEXT,
    embedding   VECTOR(1024),
    meta        JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kg_observations (
    id            SERIAL PRIMARY KEY,
    entity_id     INT REFERENCES kg_entities(id) ON DELETE CASCADE,
    content       TEXT NOT NULL,
    content_hash  TEXT GENERATED ALWAYS AS (encode(digest(content, 'sha256'), 'hex')) STORED,
    embedding     VECTOR(1024),
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    source_agent  TEXT,
    ref_doc_id    INT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kg_relations (
    id              SERIAL PRIMARY KEY,
    from_entity_id  INT REFERENCES kg_entities(id) ON DELETE CASCADE,
    to_entity_id    INT REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(from_entity_id, to_entity_id, relation_type)
);

-- 知识图谱索引
CREATE INDEX IF NOT EXISTS idx_kg_entities_embedding ON kg_entities USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_kg_observations_embedding ON kg_observations USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_kg_observations_fts ON kg_observations USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_kg_observations_entity ON kg_observations (entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_observations_hash ON kg_observations (content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_observations_dedup ON kg_observations (entity_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_kg_relations_from ON kg_relations (from_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_relations_to ON kg_relations (to_entity_id);

-- ============================================================
-- 对话存档层 (chat_ 前缀)
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_sessions (
    id                SERIAL PRIMARY KEY,
    agent             TEXT NOT NULL,
    native_session_id TEXT,
    project_dir       TEXT,
    model             TEXT,
    message_count     INT DEFAULT 0,
    started_at        TIMESTAMPTZ,
    ended_at          TIMESTAMPTZ,
    meta              JSONB DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent, native_session_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id             SERIAL PRIMARY KEY,
    session_id     INT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    content        TEXT,
    meta           JSONB DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL,
    search_vector  TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', COALESCE(content, ''))) STORED
);

CREATE TABLE IF NOT EXISTS chat_attachments (
    id          SERIAL PRIMARY KEY,
    message_id  INT REFERENCES chat_messages(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    file_type   TEXT,
    file_size   INT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 对话存档索引
CREATE INDEX IF NOT EXISTS idx_chat_sessions_agent ON chat_sessions (agent);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_started ON chat_sessions (started_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages (session_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages (created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_fts ON chat_messages USING GIN (search_vector);

