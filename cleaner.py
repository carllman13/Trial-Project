"""Strip disclaimers from `content`.

Runs after the splitter, on `content` rather than the raw body -- a disclaimer
sits inside the sender's own new text, not in the quoted chain.

Entirely local: no network, no Outlook. Safe to re-run as often as you like.
"""
import json
import re
import zlib

import textnorm

CLEANER_CODE_VERSION = 2

DEFAULT_PATTERNS = [
    ("Mobile signature (iPhone)", "Sent from my iPhone", "literal"),
    ("Mobile signature (Android)", "Sent from my Android", "literal"),
    ("Outlook mobile signature", "Get Outlook for iOS", "literal"),
]


def build_matcher(pattern_text, kind="literal"):
    r"""Compile one disclaimer pattern.

    Literal patterns match whitespace-blind. The same footer arrives wrapped
    differently from Outlook desktop, mobile and web -- identical words,
    different line breaks -- so an exact string match misses most copies.
    Joining the words with \s+ makes one stored pattern match every wrapping.
    """
    if kind == "regex":
        return re.compile(pattern_text, re.IGNORECASE | re.DOTALL)
    words = textnorm.normalize(pattern_text).split()
    if not words:
        raise ValueError("pattern is empty")
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)


def load_patterns(db):
    """Return compiled enabled patterns and errors; callers decide whether to proceed."""
    usable, broken = [], []
    for pattern_id, label, text, kind in db.execute(
        "SELECT pattern_id, label, pattern_text, kind FROM disclaimer_patterns "
        "WHERE enabled = 1 ORDER BY pattern_id"
    ):
        try:
            usable.append((pattern_id, label, build_matcher(text, kind)))
        except (re.error, ValueError) as exc:
            broken.append((pattern_id, label, str(exc)))
    return usable, broken


def fingerprint(patterns):
    """Code version plus the exact pattern set, as one integer.

    Add, edit or disable a pattern and every message goes stale automatically.
    """
    settings = [
        CLEANER_CODE_VERSION, textnorm.CODE_VERSION,
        [[pid, matcher.pattern, matcher.flags] for pid, _label, matcher in patterns],
    ]
    seed = json.dumps(settings, ensure_ascii=True, separators=(",", ":"))
    return zlib.crc32(seed.encode("utf-8")) & 0x7FFFFFFF


def clean_text(content, patterns):
    """Remove every enabled disclaimer from one message.

    Returns (cleaned, [(pattern_id, chars_removed), ...]).
    Patterns run in database order; overlapping matches can depend on that order.
    """
    if not content:
        return "", []
    text, hits = content, []
    for pattern_id, _label, matcher in patterns:
        stripped, count = matcher.subn("", text)
        if count:
            hits.append((pattern_id, len(text) - len(stripped)))
            text = stripped
    return textnorm.normalize(text), hits


def clean_messages(db, rebuild=False, dry_run=False, log=print):
    """Clean every message whose cleaned_content is stale."""
    patterns, broken = load_patterns(db)
    for pattern_id, label, err in broken:
        log(f"  ! pattern {pattern_id} ({label}) failed to compile: {err}")
    if broken:
        raise ValueError("Invalid disclaimer patterns; fix or disable them before cleaning.")
    # An empty enabled set restores content and removes old hit records.

    version = fingerprint(patterns)
    where = "" if rebuild else \
        "AND (cleaner_version IS NULL OR cleaner_version != :v)"
    rows = db.execute(
        f"SELECT msg_key, content FROM messages "
        f"WHERE content IS NOT NULL {where}", {"v": version}
    ).fetchall()

    if not rows:
        log(f"up to date (version {version}); nothing stale")
        return 0

    changed = emptied = 0
    for msg_key, content in rows:
        cleaned, hits = clean_text(content, patterns)
        if cleaned != content:
            changed += 1
        if content.strip() and not cleaned.strip():
            emptied += 1
            log(f"  ! {msg_key} cleaned to empty -- pattern too greedy?")
        if dry_run:
            continue
        db.execute(
            "UPDATE messages SET cleaned_content = ?, cleaner_version = ? "
            "WHERE msg_key = ?",
            (cleaned, version, msg_key),
        )
        db.execute("DELETE FROM disclaimer_hits WHERE msg_key = ?", (msg_key,))
        db.executemany(
            "INSERT INTO disclaimer_hits (msg_key, pattern_id, chars_removed) "
            "VALUES (?,?,?)",
            [(msg_key, pid, n) for pid, n in hits],
        )
    if not dry_run:
        db.commit()

    log(f"{'would clean' if dry_run else 'cleaned'} {len(rows)} message(s); "
        f"{changed} changed; version {version}")
    if emptied:
        log(f"  {emptied} message(s) ended up empty -- worth a look")
    return len(rows)


def add_pattern(db, label, pattern_text, kind="literal"):
    """Add a disclaimer. Paste the text as it appears on screen; line breaks
    do not matter for literal patterns."""
    build_matcher(pattern_text, kind)          # fail here, not mid-run
    db.execute(
        "INSERT INTO disclaimer_patterns (label, pattern_text, kind, created_at) "
        "VALUES (?,?,?,datetime('now'))",
        (label, pattern_text, kind),
    )
    db.commit()
