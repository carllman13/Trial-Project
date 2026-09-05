-- Outlook -> SQLite pipeline. Three tables plus one audit table.
--
-- Rule of thumb used throughout:
--   raw columns are written once at ingest and never modified
--   derived columns can be deleted and rebuilt at any time

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS messages (
    msg_key          TEXT PRIMARY KEY,   -- internetMessageId; stable across folder moves
    conversation_id  TEXT,
    subject          TEXT,
    sender_name      TEXT,
    sender_addr      TEXT,
    sent_time        TEXT,               -- ISO8601 UTC
    received_time    TEXT,

    -- raw: never overwrite these
    body_raw         TEXT,               -- full body incl. quoted chain
    body_type        TEXT,               -- 'html' | 'text'
    unique_body_raw  TEXT,               -- Graph uniqueBody: this message's new text

    -- derived: safe to rebuild
    content          TEXT,               -- plaintext of unique_body_raw
    quoted_history   TEXT,               -- what body had that unique_body didn't
    cleaned_content  TEXT,               -- content minus disclaimers
    cleaner_version  INTEGER,            -- which cleaning pass produced cleaned_content

    first_ingested   TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    msg_key  TEXT NOT NULL REFERENCES messages(msg_key) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('from','to','cc','bcc')),
    addr     TEXT NOT NULL,              -- lowercased at ingest
    name     TEXT,
    PRIMARY KEY (msg_key, role, addr)
);
CREATE INDEX IF NOT EXISTS idx_participants_addr ON participants(addr);

CREATE TABLE IF NOT EXISTS disclaimer_patterns (
    pattern_id    INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,         -- for you, e.g. 'Acme legal footer v2'
    pattern_text  TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'literal' CHECK (kind IN ('literal','regex')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    updated_at    TEXT
);

-- Which pattern stripped what, per message. Cheap, and the only way to answer
-- "why did this email clean to empty" and "which patterns never fire".
CREATE TABLE IF NOT EXISTS disclaimer_hits (
    msg_key        TEXT NOT NULL REFERENCES messages(msg_key) ON DELETE CASCADE,
    pattern_id     INTEGER NOT NULL REFERENCES disclaimer_patterns(pattern_id),
    chars_removed  INTEGER NOT NULL,
    PRIMARY KEY (msg_key, pattern_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_stale ON messages(cleaner_version);
