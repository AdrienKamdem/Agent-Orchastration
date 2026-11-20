-- Initial ingestion schema. See docs/ingestion/PLAN.md section 5.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE sources (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    config       JSONB NOT NULL DEFAULT '{}',
    cursor       JSONB,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id             BIGSERIAL PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES sources(id),
    external_id    TEXT NOT NULL,
    version        INT  NOT NULL DEFAULT 1,
    url            TEXT,
    title          TEXT,
    published_at   TIMESTAMPTZ,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash   TEXT NOT NULL,
    mime_type      TEXT,
    raw_path       TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('fetched', 'parsed', 'chunked', 'embedded', 'failed')),
    error          TEXT,
    parser_version TEXT,
    metadata       JSONB NOT NULL DEFAULT '{}',
    UNIQUE (source_id, external_id, version)
);
CREATE INDEX documents_status_idx ON documents (status);
CREATE INDEX documents_ticker_idx ON documents ((metadata->>'ticker'));
CREATE INDEX documents_published_idx ON documents (published_at DESC);

CREATE TABLE chunks (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal         INT NOT NULL,
    text            TEXT NOT NULL,
    token_count     INT NOT NULL,
    section         TEXT,
    page            INT,
    char_start      INT NOT NULL,
    char_end        INT NOT NULL,
    chunker         TEXT NOT NULL,
    embedding_model TEXT,
    -- 384 = bge-small-en-v1.5. Comparing models with other dimensions is a week-4
    -- problem (one embedding table per model); see the plan.
    embedding       vector(384),
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (document_id, chunker, ordinal)
);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);

CREATE TABLE ingestion_runs (
    id           BIGSERIAL PRIMARY KEY,
    source_id    TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    stats        JSONB,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'ok', 'partial', 'failed'))
);
