"""Strip disclaimers from message content.

Runs entirely against the local database -- no network, no Outlook. Safe to
re-run as often as you like: cleaned_content is derived, and every row is
rebuilt whenever the code or the pattern list changes.

    python3 clean.py mail.db              # clean whatever is stale
    python3 clean.py mail.db --dry-run    # show what would change
    python3 clean.py mail.db --rebuild    # redo every row
    python3 clean.py mail.db --report     # which patterns actually fire
"""
import argparse
import re
import sqlite3
import zlib

import textnorm

# Bump when the matching logic itself changes. Pattern edits are picked up
# automatically -- see current_version().
CODE_VERSION = 1


def build_matcher(pattern_text: str, kind: str):
    """Compile one pattern.

    Literal patterns are matched whitespace-blind: the same disclaimer gets
    line-wrapped differently by Outlook desktop, mobile and web, so an exact
    string match on stored text misses most of the time. Every run of
    whitespace in the pattern becomes "one or more whitespace".
    """
    if kind == "regex":
        return re.compile(pattern_text, re.IGNORECASE | re.DOTALL)
    tokens = pattern_text.split()
    if not tokens:
        raise ValueError("empty pattern")
    return re.compile(r"\s+".join(re.escape(t) for t in tokens), re.IGNORECASE)


def load_patterns(db):
    """Enabled patterns, compiled. Bad patterns are reported, not fatal."""
    out, broken = [], []
    for pid, label, text, kind in db.execute(
        "SELECT pattern_id, label, pattern_text, kind FROM disclaimer_patterns "
        "WHERE enabled = 1 ORDER BY pattern_id"
    ):
        try:
            out.append((pid, label, build_matcher(text, kind)))
        except (re.error, ValueError) as exc:
            broken.append((pid, label, str(exc)))
    return out, broken


def current_version(patterns) -> int:
    """Identity of this cleaning setup: the code plus the exact pattern set.

    Storing this in cleaner_version means adding, editing or disabling a
    pattern automatically marks every row stale. Nothing to remember to bump.
    """
    seed = str(CODE_VERSION) + "|" + "|".join(
        f"{pid}:{rx.pattern}" for pid, _, rx in patterns
    )
    return zlib.crc32(seed.encode()) & 0x7FFFFFFF


def clean_text(content: str, patterns):
    """Remove every enabled disclaimer. Returns (cleaned, [(pattern_id, chars)])."""
    if not content:
        return "", []
    text, hits = content, []
    for pid, _label, rx in patterns:
        stripped, n = rx.subn("", text)
        if n:
            hits.append((pid, len(text) - len(stripped)))
            text = stripped
    return textnorm.normalize(text), hits


def run(db, rebuild=False, dry_run=False):
    patterns, broken = load_patterns(db)
    for pid, label, err in broken:
        print(f"  ! pattern {pid} ({label}) failed to compile: {err}")
    if not patterns:
        print("no usable patterns; nothing to do")
        return

    version = current_version(patterns)
    if rebuild:
        rows = db.execute(
            "SELECT msg_key, content FROM messages WHERE content IS NOT NULL"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT msg_key, content FROM messages "
            "WHERE content IS NOT NULL AND (cleaner_version IS NULL OR cleaner_version != ?)",
            (version,),
        ).fetchall()

    if not rows:
        print(f"up to date (version {version}); nothing stale")
        return

    changed = emptied = 0
    for msg_key, content in rows:
        cleaned, hits = clean_text(content, patterns)
        if cleaned != content:
            changed += 1
        if content.strip() and not cleaned.strip():
            emptied += 1
            print(f"  ! {msg_key} cleaned to empty -- pattern too greedy?")
        if dry_run:
            continue
        db.execute(
            "UPDATE messages SET cleaned_content = ?, cleaner_version = ? WHERE msg_key = ?",
            (cleaned, version, msg_key),
        )
        db.execute("DELETE FROM disclaimer_hits WHERE msg_key = ?", (msg_key,))
        db.executemany(
            "INSERT INTO disclaimer_hits (msg_key, pattern_id, chars_removed) VALUES (?,?,?)",
            [(msg_key, pid, n) for pid, n in hits],
        )
    if not dry_run:
        db.commit()

    verb = "would clean" if dry_run else "cleaned"
    print(f"{verb} {len(rows)} message(s); {changed} changed; version {version}")
    if emptied:
        print(f"  {emptied} message(s) ended up empty -- worth a look")


def report(db):
    print("pattern                                    msgs    chars")
    print("-" * 60)
    for label, msgs, chars in db.execute(
        """SELECT p.label, COUNT(h.msg_key), COALESCE(SUM(h.chars_removed), 0)
           FROM disclaimer_patterns p
           LEFT JOIN disclaimer_hits h ON h.pattern_id = p.pattern_id
           WHERE p.enabled = 1
           GROUP BY p.pattern_id ORDER BY COUNT(h.msg_key) DESC"""
    ):
        flag = "   <- never fires" if msgs == 0 else ""
        print(f"{label[:40]:<40} {msgs:>6} {chars:>8}{flag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--rebuild", action="store_true", help="redo every row")
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--report", action="store_true", help="per-pattern hit counts")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA foreign_keys = ON")
    if args.report:
        report(db)
    else:
        run(db, rebuild=args.rebuild, dry_run=args.dry_run)
    db.close()


if __name__ == "__main__":
    main()
