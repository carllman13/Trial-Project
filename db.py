"""Open a database, create the schema, seed the default patterns."""
import os
import sqlite3

import cleaner
import splitter

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path, log=print):
    """Create tables if missing and seed defaults. Safe to run repeatedly."""
    conn = connect(path)
    # Before the schema, not after: schema.sql builds indexes over columns that
    # an older database may not have yet, and would fail on the way past.
    migrate(conn)
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    added_b = seed_boundary_patterns(conn)
    added_d = seed_disclaimer_patterns(conn)
    conn.commit()
    log(f"schema ready at {path}"
        + (f"; seeded {added_b} boundary pattern(s)" if added_b else "")
        + (f"; seeded {added_d} disclaimer pattern(s)" if added_d else ""))
    return conn


def migrate(conn):
    """Add columns that a database created by an earlier version is missing.

    CREATE TABLE IF NOT EXISTS leaves an existing table alone, so new columns
    have to be added by hand.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if not have:
        return                       # brand new database; schema.sql handles it
    if "recipients_dropped" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN recipients_dropped "
                     "INTEGER DEFAULT 0")
    if "splitter_version" in have and "boundary_patterns_version" not in have:
        conn.execute("ALTER TABLE messages RENAME COLUMN splitter_version "
                     "TO boundary_patterns_version")


def seed_boundary_patterns(conn):
    """Insert built-in markers. Existing rows are left alone, so your own
    edits survive re-running init."""
    added = 0
    for p in splitter.DEFAULT_PATTERNS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO boundary_patterns "
            "(label, line_regex, require_regex, confirm_regex, confirm_within, "
            " priority, example_block, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
            (p["label"], p["line_regex"], p.get("require_regex"),
             p.get("confirm_regex"), p.get("confirm_within", 5),
             p.get("priority", 100), p["example_block"], p.get("notes")),
        )
        added += cur.rowcount
    return added


def seed_disclaimer_patterns(conn):
    added = 0
    for label, text, kind in cleaner.DEFAULT_PATTERNS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO disclaimer_patterns "
            "(label, pattern_text, kind, created_at) VALUES (?,?,?,datetime('now'))",
            (label, text, kind),
        )
        added += cur.rowcount
    return added


def store_message(conn, msg, participants=()):
    """Insert or replace one message and its participants.

    `msg` sets only RAW columns; the derived ones are left NULL so the
    splitter and cleaner pick the message up on their next run.
    """
    row = {"conversation_id": None, "subject": None, "sender_name": None,
           "sender_addr": None, "sent_time": None, "received_time": None,
           "folder": None, "has_attachments": 0, "body_type": "html",
           "recipients_dropped": 0, **msg}
    # Lowercase here as well as in participants, or the same person shows up
    # twice in SELECT DISTINCT sender_addr.
    if row["sender_addr"]:
        row["sender_addr"] = row["sender_addr"].lower()
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(msg_key, conversation_id, subject, sender_name, sender_addr, "
        " sent_time, received_time, folder, has_attachments, "
        " recipients_dropped, body_raw, body_type, first_ingested) "
        "VALUES (:msg_key, :conversation_id, :subject, :sender_name, "
        "        :sender_addr, :sent_time, :received_time, :folder, "
        "        :has_attachments, :recipients_dropped, :body_raw, "
        "        :body_type, datetime('now'))",
        row,
    )
    conn.execute("DELETE FROM participants WHERE msg_key = ?", (msg["msg_key"],))
    conn.executemany(
        "INSERT OR IGNORE INTO participants (msg_key, role, addr, name) "
        "VALUES (?,?,?,?)",
        [(msg["msg_key"], role, addr.lower(), name)
         for role, addr, name in participants],
    )
