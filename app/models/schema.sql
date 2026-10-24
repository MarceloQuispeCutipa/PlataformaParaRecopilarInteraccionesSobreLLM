-- Esquema SQLite. Fuente oficial de datos; los CSV/JSONL son solo copias exportadas.
-- Todas las marcas temporales: ISO 8601 en UTC (p. ej. 2026-01-01T10:00:00.000+00:00).

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,                       -- seudónimo (uuid), sin datos personales
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled'))
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id      TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title                TEXT,
    created_at           TEXT NOT NULL,
    last_activity_at     TEXT NOT NULL,
    explicitly_closed_at TEXT,
    status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    model_name           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, last_activity_at);

CREATE TABLE IF NOT EXISTS sessions (
    session_id                TEXT PRIMARY KEY,
    conversation_id           TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    user_id                   TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    previous_session_id       TEXT,                    -- vínculo con la sesión anterior de la conversación
    started_at                TEXT NOT NULL,
    last_activity_at          TEXT NOT NULL,           -- última interacción REAL
    ended_at                  TEXT,                    -- inactividad: = última actividad; explícita: = momento del clic
    end_reason                TEXT CHECK (end_reason IS NULL OR end_reason IN ('explicit_user_action','inactivity_timeout')),
    inactivity_detected_at    TEXT,                    -- cuándo el servidor detectó el umbral (distinto de ended_at)
    status                    TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','ended')),
    total_user_messages       INTEGER NOT NULL DEFAULT 0,
    total_assistant_messages  INTEGER NOT NULL DEFAULT 0,
    total_duration_seconds    REAL,                    -- ended_at - started_at (NULL mientras está activa)
    message_threshold_reached INTEGER NOT NULL DEFAULT 0,
    satisfaction_status       TEXT NOT NULL DEFAULT 'unknown' CHECK (satisfaction_status IN
        ('explicitly_satisfied','likely_satisfied','unresolved','abandoned_or_inactive','unknown')),
    satisfaction_evidence     TEXT,                    -- JSON con la evidencia observable
    satisfaction_method       TEXT,
    satisfaction_confidence   REAL,
    satisfaction_evaluated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_conversation ON sessions(conversation_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(status, last_activity_at);
-- A lo sumo una sesión activa por conversación (protege ante concurrencia)
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_session ON sessions(conversation_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS messages (
    message_id              TEXT PRIMARY KEY,
    event_id                TEXT NOT NULL,
    user_id                 TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    conversation_id         TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    session_id              TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    parent_message_id       TEXT REFERENCES messages(message_id) ON DELETE CASCADE, -- respuesta -> consulta
    seq                     INTEGER NOT NULL,          -- orden dentro de la conversación
    role                    TEXT NOT NULL CHECK (role IN ('user','assistant')),
    message_type            TEXT,                      -- query | instruction | follow_up (heurístico)
    content                 TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    generation_started_at   TEXT,
    generation_completed_at TEXT,
    response_time_ms        INTEGER,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    model_name              TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('processing','generating','completed','failed','interrupted')),
    error_info              TEXT,
    client_message_id       TEXT,                      -- idempotencia ante reintentos
    UNIQUE (conversation_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_message ON messages(conversation_id, client_message_id)
    WHERE client_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(status) WHERE status IN ('processing','generating');

CREATE TABLE IF NOT EXISTS interaction_events (
    event_id        TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    session_id      TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_time ON interaction_events(event_timestamp);
CREATE INDEX IF NOT EXISTS idx_events_session ON interaction_events(session_id, event_timestamp);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id     TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    rating          TEXT NOT NULL CHECK (rating IN ('useful','not_useful')),
    goal_completed  INTEGER CHECK (goal_completed IS NULL OR goal_completed IN (0,1)),
    comment         TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (message_id, user_id)
);

-- Registro de exportaciones (tabla auxiliar; no toca los datos de interacción)
CREATE TABLE IF NOT EXISTS export_log (
    export_id    TEXT PRIMARY KEY,
    requested_by TEXT NOT NULL,                        -- admin | scheduler
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('ok','failed')),
    output_dir   TEXT,
    files_json   TEXT,
    filters_json TEXT,
    error        TEXT
);
