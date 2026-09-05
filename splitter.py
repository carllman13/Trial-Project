"""Find where the quoted chain begins, so the sender's new text stands alone.

Only the FIRST boundary matters: a message ends where the next one begins.
Everything above it is `content`, everything from it down is `quoted_history`.
Finding no boundary is a normal outcome, not an error -- the unsplit messages
are the to-do list of marker formats still to add.

Markers are recognised by their FRAME, never parsed. Real examples:

    On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:
    On Sun, Aug 9, 2026 at 8:49 PM David Salfati <ds.ext@blackfuel.ai> wrote:
    On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain CFA, CAIA <B@h.com> wrote:

Dates, names and credentials all vary; commas appear inside names. One loose
regex over the frame handles every one of them.
"""
import re
import zlib

import textnorm

# Bump when the matching logic changes. Pattern edits are picked up
# automatically via the fingerprint.
SPLITTER_CODE_VERSION = 1

# A wrapped marker spans more than one line, so each position is tried as
# 1 line, then 2 joined, then 3. Trying 1 first is what keeps an unwrapped
# marker from being ruined by body text on the following line.
MAX_JOIN = 3

_ADDR = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")

# Seeded on first init. Each carries a real snippet it must cut at line 0.
DEFAULT_PATTERNS = [
    dict(
        label="Outlook header block (EN)",
        line_regex=r"^\s*From:\s*\S",
        confirm_regex=r"^\s*(Sent|Date)\s*:",
        priority=10,
        example_block=(
            "From: Jane Patel <jane.patel@acme.com>\n"
            "Sent: 03 September 2026 08:30\n"
            "To: Carl Wei\n"
            "Subject: RE: vendor onboarding"
        ),
        notes="Outlook's own reply/forward header. 'From:' alone is common in "
              "ordinary prose, so a following 'Sent:'/'Date:' is required.",
    ),
    dict(
        label="Outlook header block (FR)",
        line_regex=r"^\s*De\s*:\s*\S",
        confirm_regex=r"^\s*(Envoyé|Envoye|Date)\s*:",
        priority=10,
        example_block=(
            "De : Jane Patel <jane.patel@acme.com>\n"
            "Envoyé : 3 septembre 2026 08:30\n"
            "Objet : RE: vendor onboarding"
        ),
        notes="Unverified against real French mail -- check before trusting.",
    ),
    dict(
        label="Outlook header block (DE)",
        line_regex=r"^\s*Von\s*:\s*\S",
        confirm_regex=r"^\s*(Gesendet|Datum)\s*:",
        priority=10,
        example_block=(
            "Von: Jane Patel <jane.patel@acme.com>\n"
            "Gesendet: 3. September 2026 08:30\n"
            "Betreff: RE: vendor onboarding"
        ),
        notes="Unverified against real German mail -- check before trusting.",
    ),
    dict(
        label="Gmail / Apple / mobile On-wrote",
        line_regex=r"^\s*On\b.*\bwrote:\s*$",
        require_regex=r"<[^<>@\s]+@[^<>\s]+>",
        priority=20,
        example_block=(
            "On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:\n"
            "> earlier text"
        ),
        notes="An address on the same line is required, otherwise the sentence "
              "'On the topic of the memo he wrote:' matches.",
    ),
    dict(
        label="Original message marker",
        line_regex=r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
        priority=5,
        example_block="----- Original Message -----\nFrom: someone",
    ),
    dict(
        label="Forwarded message marker",
        line_regex=r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$",
        priority=5,
        example_block="---------- Forwarded message ----------\nFrom: someone",
    ),
    dict(
        label="Outlook underscore separator",
        line_regex=r"^\s*_{10,}\s*$",
        confirm_regex=r"^\s*(From|De|Von)\s*:",
        confirm_within=3,
        priority=1,
        example_block=(
            "________________________________\n"
            "From: Jane Patel <jane.patel@acme.com>\n"
            "Sent: 03 September 2026 08:30"
        ),
        notes="Sits just above the header block, so it cuts marginally earlier.",
    ),
    dict(
        label="Quoted line run",
        line_regex=r"^\s*>",
        confirm_regex=r"^\s*>",
        confirm_within=2,
        priority=90,
        example_block="> earlier text\n> more earlier text",
        notes="Last resort: two consecutive '>' lines. Low priority so a real "
              "marker above them wins.",
    ),
]


class Pattern:
    """One compiled boundary pattern."""

    __slots__ = ("pattern_id", "label", "line", "require", "confirm",
                 "confirm_within", "priority")

    def __init__(self, pattern_id, label, line_regex, require_regex,
                 confirm_regex, confirm_within, priority):
        self.pattern_id = pattern_id
        self.label = label
        self.line = re.compile(line_regex, re.IGNORECASE)
        self.require = re.compile(require_regex, re.IGNORECASE) if require_regex else None
        self.confirm = re.compile(confirm_regex, re.IGNORECASE) if confirm_regex else None
        self.confirm_within = confirm_within or 5
        self.priority = priority


def load_patterns(db):
    """Enabled patterns, compiled. Malformed ones are reported, not fatal."""
    usable, broken = [], []
    rows = db.execute(
        "SELECT pattern_id, label, line_regex, require_regex, confirm_regex, "
        "       confirm_within, priority "
        "FROM boundary_patterns WHERE enabled = 1 ORDER BY priority, pattern_id"
    )
    for row in rows:
        try:
            usable.append(Pattern(*row))
        except re.error as exc:
            broken.append((row[0], row[1], str(exc)))
    return usable, broken


def fingerprint(patterns):
    """Identity of this splitter setup: code version plus the exact pattern set.

    Stored on each message. Change a pattern and every message goes stale and
    re-splits on the next run -- no version number to remember to bump.
    """
    seed = str(SPLITTER_CODE_VERSION) + "|" + "|".join(
        "{}:{}:{}:{}".format(
            p.pattern_id, p.line.pattern,
            p.require.pattern if p.require else "",
            p.confirm.pattern if p.confirm else "",
        )
        for p in patterns
    )
    return zlib.crc32(seed.encode()) & 0x7FFFFFFF


def split(body_text, patterns):
    """Cut a body at its first boundary.

    Returns (content, quoted_history, pattern_id, quoted_from_addr).
    pattern_id is None when no boundary was found, and the whole body is
    content.
    """
    if not body_text:
        return "", "", None, None

    lines = body_text.split("\n")
    for i in range(len(lines)):
        for span in range(1, MAX_JOIN + 1):
            if i + span > len(lines):
                break
            logical = textnorm.collapse(" ".join(lines[i:i + span]))
            if not logical:
                continue
            for p in patterns:                      # already ordered by priority
                if not p.line.search(logical):
                    continue
                if p.require and not p.require.search(logical):
                    continue
                if p.confirm:
                    window = lines[i + span : i + span + p.confirm_within]
                    if not any(p.confirm.search(l) for l in window):
                        continue
                addr = _ADDR.search(logical)
                return (
                    textnorm.normalize("\n".join(lines[:i])),
                    textnorm.normalize("\n".join(lines[i:])),
                    p.pattern_id,
                    addr.group(1).lower() if addr else None,
                )
    return textnorm.normalize(body_text), "", None, None


def split_messages(db, rebuild=False, dry_run=False, log=print):
    """Split every message whose result is stale. `body_raw` is never touched.

    Re-splitting changes `content`, so the cleaner's output is invalidated too.
    """
    patterns, broken = load_patterns(db)
    for pattern_id, label, err in broken:
        log(f"  ! pattern {pattern_id} ({label}) failed to compile: {err}")
    if not patterns:
        log("no usable boundary patterns; nothing to do")
        return 0

    version = fingerprint(patterns)
    where = "" if rebuild else \
        "AND (splitter_version IS NULL OR splitter_version != :v)"
    rows = db.execute(
        f"SELECT msg_key, body_raw, body_type FROM messages "
        f"WHERE body_raw IS NOT NULL {where}", {"v": version}
    ).fetchall()

    if not rows:
        log(f"up to date (version {version}); nothing stale")
        return 0

    unsplit = 0
    for msg_key, body_raw, body_type in rows:
        text = textnorm.to_text(body_raw, body_type)
        content, history, pattern_id, addr = split(text, patterns)
        if pattern_id is None:
            unsplit += 1
        if dry_run:
            continue
        db.execute(
            "UPDATE messages SET content = ?, quoted_history = ?, "
            "       boundary_pattern_id = ?, quoted_from_addr = ?, "
            "       splitter_version = ?, cleaner_version = NULL "
            "WHERE msg_key = ?",
            (content, history, pattern_id, addr, version, msg_key),
        )
    if not dry_run:
        db.commit()

    log(f"{'would split' if dry_run else 'split'} {len(rows)} message(s); "
        f"{unsplit} with no boundary found; version {version}")
    if unsplit:
        log("  run `report` to sample them -- each new marker is one more row")
    return len(rows)


def test_patterns(db, log=print):
    """Check every pattern cuts its own example_block at line 0, in isolation.

    New patterns arrive written from a screenshot. A regex that looks correct
    but matches nothing is otherwise invisible: it just never fires.
    """
    failures = 0
    rows = db.execute(
        "SELECT pattern_id, label, line_regex, require_regex, confirm_regex, "
        "       confirm_within, priority, example_block "
        "FROM boundary_patterns WHERE enabled = 1 ORDER BY pattern_id"
    ).fetchall()
    for row in rows:
        label, example = row[1], row[7]
        try:
            pattern = Pattern(*row[:7])
        except re.error as exc:
            log(f"  FAIL {label}: regex does not compile: {exc}")
            failures += 1
            continue
        content, _hist, pattern_id, _addr = split(example, [pattern])
        if pattern_id is None:
            log(f"  FAIL {label}: does not match its own example")
            failures += 1
        elif content:
            log(f"  FAIL {label}: cuts at the wrong line, left {content!r}")
            failures += 1
        else:
            log(f"  ok   {label}")
    log(f"{len(rows) - failures}/{len(rows)} patterns pass")
    return failures
