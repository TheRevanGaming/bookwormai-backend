PRAGMA foreign_keys = ON;

-- 1. Projects
CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    description     TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. Sessions & Conversation Memory
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT,
    summary         TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,   -- 'user' | 'assistant' | 'system'
    content         TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 3. Docs (Lore, Rules, Systems, Lyrics, etc.)
CREATE TABLE IF NOT EXISTS docs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    tags            TEXT,
    canon_state     TEXT NOT NULL DEFAULT 'LOCKED_CANON',
    source          TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doc_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    body            TEXT NOT NULL,
    changed_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    change_reason   TEXT
);

CREATE TABLE IF NOT EXISTS doc_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    embedding       BLOB
);

-- 4. Entities (Characters, Realms, Flora, Fauna, Biomes, etc.)
CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL,
    -- examples:
    -- 'character','realm','biome','flora','fauna','culture',
    -- 'language','faction','item','species','magic_system',
    -- 'tech_system','geological_feature','material','resource',
    -- 'song','album','artist_persona','motif'
    summary         TEXT,
    tags            TEXT,
    canon_state     TEXT NOT NULL DEFAULT 'LOCKED_CANON',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entity_fields (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    field_value     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS entity_embeddings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    embedding       BLOB
);

-- 5. Events & Timeline
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT,
    era             TEXT,
    start_index     INTEGER,
    end_index       INTEGER,
    canon_state     TEXT NOT NULL DEFAULT 'LOCKED_CANON',
    tags            TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS event_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id       INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    role            TEXT,
    notes           TEXT
);

-- 6. Users & Preferences
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT
);

CREATE TABLE IF NOT EXISTS user_prefs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL
);

-- 7. Audio System (Voices, Audio Assets, Sound Presets)
CREATE TABLE IF NOT EXISTS voice_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    role            TEXT,
    gender          TEXT,
    age_descriptor  TEXT,
    tone            TEXT,
    accent          TEXT,
    style_notes     TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audio_assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    asset_type      TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    duration_sec    REAL,
    related_doc_id  INTEGER REFERENCES docs(id) ON DELETE SET NULL,
    related_entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    voice_profile_id  INTEGER REFERENCES voice_profiles(id) ON DELETE SET NULL,
    tags            TEXT,
    generation_prompt TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sound_presets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    p

python3 - << 'EOF'
import sqlite3, pathlib

db_path = pathlib.Path("bookworm.db")
schema_path = pathlib.Path("db/schema.sql")

conn = sqlite3.connect(db_path)
with open(schema_path, "r", encoding="utf-8") as f:
    conn.executescript(f.read())
conn.close()
print("Initialized bookworm.db using db/schema.sql")
