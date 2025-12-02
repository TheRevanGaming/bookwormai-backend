-- db/schema.sql
-- Core schema for Book Worm AI: projects, docs, analytics events

PRAGMA foreign_keys = ON;

-- Projects: high-level containers (books, games, worlds, etc.)
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Docs: canon / lore / systems / chapters stored under projects
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT,
    canon_state TEXT DEFAULT 'LOCKED_CANON',
    source      TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- Analytics events: records of how Book Worm is used
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT DEFAULT (datetime('now')),
    event_type  TEXT NOT NULL,     -- e.g. 'GENERATE', 'GENERATE_IMAGE', 'ERROR'
    user_id     TEXT,              -- optional: anonymous id / owner / etc.
    user_tier   TEXT,              -- e.g. 'free', 'basic', 'pro', 'owner'
    metadata    TEXT               -- JSON blob as TEXT
);
