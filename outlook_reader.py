"""Outlook -> SQLite reader.

Two halves, deliberately independent:

  FETCH    slow, needs the network, runs once per email. Writes the raw
           columns (body_raw, unique_body_raw) and never touches them again.
           -- not written yet --

  CLEAN    instant, local, run as often as you like. Reads `content`, removes
           the disclaimers listed in the database, writes `cleaned_content`.

Standard library only, so there is nothing to install on a locked-down machine.

    python3 outlook_reader.py mail.db --clean
    python3 outlook_reader.py mail.db --clean --dry-run
    python3 outlook_reader.py mail.db --clean --rebuild
    python3 outlook_reader.py mail.db --report
"""
import argparse
import re
import sqlite3
import zlib
from html.parser import HTMLParser

# Bump only when the matching logic below changes. Edits to the pattern list
# are picked up automatically -- see cleaner_fingerprint().
CLEANER_CODE_VERSION = 1


# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------

# Zero-width and soft-hyphen characters. Invisible on screen, and they sit in
# the middle of words where they silently break literal matching.
_INVISIBLE = re.compile(r"[​‌‍⁠﻿­]")
# Spaces that are not U+0020. Outlook emits non-breaking spaces constantly.
_ODD_SPACE = re.compile(r"[      ]")

_SKIP_TAGS = {"script", "style", "head", "title"}
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "table", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def normalize(text):
    """Make text safe to match patterns against.

    Folds exotic spaces to plain spaces and deletes zero-width characters:
    both are invisible when you read the email, and both stop a hand-typed
    disclaimer pattern from matching. Also collapses runs of blank lines so
    the stored text stays readable.
    """
    if not text:
        return ""
    text = _INVISIBLE.sub("", text)
    text = _ODD_SPACE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(html):
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return normalize("".join(parser.parts))


def quoted_history(body_text, unique_text):
    """Whatever the full body had that the unique body did not.

    uniqueBody strips quoted chain by shape, not by checking your mailbox, so
    history from a thread you were added to late lands here. Best effort: if
    the unique text is not found verbatim inside the body, keep the whole body
    rather than guess at a split point.
    """
    body_text = normalize(body_text)
    unique_text = normalize(unique_text)
    if not unique_text:
        return body_text
    idx = body_text.find(unique_text)
    if idx == -1:
        return body_text
    return normalize(body_text[:idx] + body_text[idx + len(unique_text):])


# --------------------------------------------------------------------------
# Disclaimer cleaning
# --------------------------------------------------------------------------

def build_matcher(pattern_text, kind="literal"):
    """Compile one disclaimer pattern.

    Literal patterns are matched whitespace-blind. The same footer arrives
    wrapped differently from Outlook desktop, mobile and web -- same words,
    different line breaks -- so an exact string match misses most copies.
    Joining the words with \\s+ makes one stored pattern match every wrapping.
    """
    if kind == "regex":
        return re.compile(pattern_text, re.IGNORECASE | re.DOTALL)
    words = normalize(pattern_text).split()
    if not words:
        raise ValueError("pattern is empty")
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)


def load_patterns(db):
    """Enabled patterns, compiled.

    Returns (usable, broken). A malformed pattern is reported and skipped
    rather than killing a run over the whole mailbox.
    """
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


def cleaner_fingerprint(patterns):
    """Identity of this cleaning setup: the code plus the exact pattern set.

    Stored on each message as cleaner_version. Add, edit or disable a pattern
    and the fingerprint changes, so every message is stale on the next run and
    gets recleaned. There is no version number to remember to bump.
    """
    seed = str(CLEANER_CODE_VERSION) + "|" + "|".join(
        f"{pid}:{matcher.pattern}" for pid, _label, matcher in patterns
    )
    return zlib.crc32(seed.encode()) & 0x7FFFFFFF


def clean_text(content, patterns):
    """Remove every enabled disclaimer from one message.

    Returns (cleaned_text, [(pattern_id, chars_removed), ...]). Patterns are
    independent, so the order they are applied in does not matter.
    """
    if not content:
        return "", []
    text, hits = content, []
    for pattern_id, _label, matcher in patterns:
        stripped, count = matcher.subn("", text)
        if count:
            hits.append((pattern_id, len(text) - len(stripped)))
            text = stripped
    return normalize(text), hits


def clean_messages(db, rebuild=False, dry_run=False, log=print):
    """Clean every message whose cleaned_content is stale.

    `content` is never modified -- cleaned_content is derived and can be
    discarded and rebuilt at any time.
    """
    patterns, broken = load_patterns(db)
    for pattern_id, label, err in broken:
        log(f"  ! pattern {pattern_id} ({label}) failed to compile: {err}")
    if not patterns:
        log("no usable patterns; nothing to do")
        return 0

    version = cleaner_fingerprint(patterns)
    if rebuild:
        rows = db.execute(
            "SELECT msg_key, content FROM messages WHERE content IS NOT NULL"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT msg_key, content FROM messages WHERE content IS NOT NULL "
            "AND (cleaner_version IS NULL OR cleaner_version != ?)",
            (version,),
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
    """Add a disclaimer. Paste the text as it appears on screen -- line breaks
    do not matter for literal patterns."""
    build_matcher(pattern_text, kind)          # fail here, not mid-run
    db.execute(
        "INSERT INTO disclaimer_patterns (label, pattern_text, kind, created_at) "
        "VALUES (?,?,?,datetime('now'))",
        (label, pattern_text, kind),
    )
    db.commit()


def pattern_report(db, log=print):
    """Per-pattern hit counts. A pattern that never fires is wrong or obsolete."""
    log("pattern                                    msgs    chars")
    log("-" * 60)
    for label, msgs, chars in db.execute(
        """SELECT p.label, COUNT(h.msg_key), COALESCE(SUM(h.chars_removed), 0)
           FROM disclaimer_patterns p
           LEFT JOIN disclaimer_hits h ON h.pattern_id = p.pattern_id
           WHERE p.enabled = 1
           GROUP BY p.pattern_id
           ORDER BY COUNT(h.msg_key) DESC"""
    ):
        flag = "   <- never fires" if msgs == 0 else ""
        log(f"{label[:40]:<40} {msgs:>6} {chars:>8}{flag}")


# --------------------------------------------------------------------------
# Fetch -- to come
# --------------------------------------------------------------------------

def fetch_messages(db):
    """Pull mail from Outlook into `messages` and `participants`.

    For each message store body_raw and unique_body_raw untouched, then derive
    content = html_to_text(unique_body_raw) and
    quoted_history = quoted_history(html_to_text(body_raw), content).
    Leave cleaned_content NULL; clean_messages() fills it.
    """
    raise NotImplementedError("fetch not written yet")


def main():
    ap = argparse.ArgumentParser(description="Outlook -> SQLite reader")
    ap.add_argument("db")
    ap.add_argument("--clean", action="store_true", help="strip disclaimers")
    ap.add_argument("--rebuild", action="store_true", help="reclean every row")
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--report", action="store_true", help="per-pattern hit counts")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA foreign_keys = ON")
    try:
        if args.report:
            pattern_report(db)
        elif args.clean:
            clean_messages(db, rebuild=args.rebuild, dry_run=args.dry_run)
        else:
            ap.error("nothing to do: pass --clean or --report")
    finally:
        db.close()


if __name__ == "__main__":
    main()
