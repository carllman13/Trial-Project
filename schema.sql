-- Outlook -> SQLite.
--
-- Two kinds of column, kept strictly apart:
--   RAW      written once at ingest, never modified
--   DERIVED  produced by a pass over the database; delete and rebuild freely
--
-- Pipeline order is: fetch -> split -> clean.

PRAGMA journal_mode = WAL;

-- One row per email actually delivered to the mailbox.
CREATE TABLE IF NOT EXISTS messages (
    msg_key             TEXT PRIMARY KEY,  -- internetMessageId; stable across folder moves
    conversation_id     TEXT,
    subject             TEXT,
    sender_name         TEXT,
    sender_addr         TEXT,
    sent_time           TEXT,              -- ISO8601 UTC
    received_time       TEXT,
    folder              TEXT,
    has_attachments     INTEGER,
    recipients_dropped  INTEGER DEFAULT 0,  -- addresses Outlook would not resolve

    -- RAW
    body_raw            TEXT,              -- whole body, quoted chain included
    body_type           TEXT,              -- 'html' | 'text'

    -- DERIVED by the splitter
    content             TEXT,              -- what this sender newly wrote
    boundary_pattern_id INTEGER REFERENCES boundary_patterns(pattern_id),
    -- Fingerprint of the enabled boundary patterns, so editing one restales
    -- every row automatically. It folds in the matching code's own version as
    -- well, so this number can also change when no pattern did -- rare, but it
    -- is why the name is not a promise about patterns alone.
    boundary_patterns_version INTEGER,

    -- DERIVED by the cleaner, from `content`
    cleaned_content     TEXT,
    cleaner_version     INTEGER,           -- same idea, for disclaimer patterns

    first_ingested      TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_unsplit  ON messages(boundary_patterns_version);
CREATE INDEX IF NOT EXISTS idx_messages_unclean  ON messages(cleaner_version);
CREATE INDEX IF NOT EXISTS idx_messages_sent     ON messages(sent_time);

-- Many people per email, so they cannot live in a column on messages.
CREATE TABLE IF NOT EXISTS participants (
    msg_key  TEXT NOT NULL REFERENCES messages(msg_key) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('from','to','cc','bcc')),
    addr     TEXT NOT NULL,                -- lowercased at ingest
    name     TEXT,
    PRIMARY KEY (msg_key, role, addr)
);
CREATE INDEX IF NOT EXISTS idx_participants_addr ON participants(addr);

-- Where a quoted chain begins. One row per marker format.
--
--   line_regex     the frame; loose on purpose
--   require_regex  must ALSO match the same logical line (kills false positives)
--   confirm_regex  must match one of the next `confirm_within` lines
--   example_block  a real snippet this pattern must cut at line 0; enforced
CREATE TABLE IF NOT EXISTS boundary_patterns (
    pattern_id      INTEGER PRIMARY KEY,
    label           TEXT NOT NULL UNIQUE,
    line_regex      TEXT NOT NULL,
    require_regex   TEXT,
    confirm_regex   TEXT,
    confirm_within  INTEGER NOT NULL DEFAULT 5,
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         INTEGER NOT NULL DEFAULT 1,
    example_block   TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT
);

-- Boilerplate to strip from `content`.
CREATE TABLE IF NOT EXISTS disclaimer_patterns (
    pattern_id    INTEGER PRIMARY KEY,
    label         TEXT NOT NULL UNIQUE,
    pattern_text  TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'literal' CHECK (kind IN ('literal','regex')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    updated_at    TEXT
);

-- Which disclaimer stripped what. The only way to answer "why is this empty"
-- and "which patterns never fire".
CREATE TABLE IF NOT EXISTS disclaimer_hits (
    msg_key        TEXT NOT NULL REFERENCES messages(msg_key) ON DELETE CASCADE,
    pattern_id     INTEGER NOT NULL REFERENCES disclaimer_patterns(pattern_id),
    chars_removed  INTEGER NOT NULL,
    PRIMARY KEY (msg_key, pattern_id)
);

-- Fetch bookkeeping: how far the last sync got.
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- The Outlook folder tree, cached here rather than in a side file so there is
-- one source of truth. entry_id/store_id let a later scan jump straight to a
-- folder instead of walking from the root; they change when a folder is moved
-- or renamed, so a failed lookup falls back to walking by path.
CREATE TABLE IF NOT EXISTS folders (
    path              TEXT PRIMARY KEY,  -- 'Store - Parent - Child', threaded down
    name              TEXT NOT NULL,
    parent_path       TEXT,              -- NULL for a store root; lets the UI build a tree
    entry_id          TEXT,
    store_id          TEXT,
    has_children      INTEGER DEFAULT 0, -- cheap Folders.Count, not a materialised walk
    children_loaded   INTEGER DEFAULT 0, -- 0 = never expanded, so the UI knows to lazy-load
    selected          INTEGER DEFAULT 0, -- unselected folders are never scanned
    last_refreshed_at TEXT,              -- per-folder watermark for "since last update"
    message_count     INTEGER,
    seen_at           TEXT               -- last time Outlook still listed this folder
);
CREATE INDEX IF NOT EXISTS idx_folders_parent   ON folders(parent_path);
CREATE INDEX IF NOT EXISTS idx_folders_selected ON folders(selected);

-- One row per run of anything. Exists so a failure on a machine we cannot see
-- survives as a queryable row rather than as terminal scrollback.
CREATE TABLE IF NOT EXISTS run_log (
    run_id      INTEGER PRIMARY KEY,
    command     TEXT NOT NULL,           -- 'refresh', 'split', 'clean', ...
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,                 -- 1 success, 0 failed, NULL still running
    summary     TEXT,                    -- one line for the UI
    detail      TEXT                     -- counts as JSON, or the error
);
