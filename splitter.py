"""Find where the quoted chain begins, so the sender's new text stands alone.

Only the FIRST boundary matters: a message ends where the next one begins.
Everything above it is `unique_body_text`; the chain below is dropped, since `body_raw`
still holds it untouched. Finding no boundary is a normal outcome, not an
error -- the unsplit messages are the to-do list of marker formats still to
add.

Markers are recognised by their FRAME, never parsed. Real examples:

    On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:
    On Sun, Aug 9, 2026 at 8:49 PM David Salfati <ds.ext@blackfuel.ai> wrote:
    On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain CFA, CAIA <B@h.com> wrote:

Dates, names and credentials all vary; commas appear inside names. One loose
regex over the frame handles every one of them.
"""
import json
import re
import zlib

import textnorm

# Bump when the matching logic changes. Pattern edits are picked up
# automatically via the fingerprint.
SPLITTER_CODE_VERSION = 2

# A wrapped marker spans more than one line, so each position is tried as
# 1 line, then 2 joined, then 3. Trying 1 first is what keeps an unwrapped
# marker from being ruined by body text on the following line.
MAX_JOIN = 3

# Localised quote markers, one row per language. To support another language,
# add a row here: db.init seeds its patterns on the next start, and existing
# rows (including your own edits) are left alone. Words are plain text, not
# regex; accented and unaccented spellings can both be listed.
#
#   from_label    Outlook header first line, e.g. "From: Jane <jane@acme.com>"
#   sent_labels   any of these must start one of the next few lines
#   original      "-----Original Message-----" wording, or None
#   wrote         Gmail/Apple attribution line as (regex, example), or None;
#                 an email address on the same line is always required
#   verified      False until checked against real mail from that language
LANGUAGES = [
    dict(code="EN", from_label="From", sent_labels=["Sent", "Date"],
         original="Original Message", verified=True,
         wrote=(r"^\s*On\b.*\bwrote:\s*$",
                "On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:")),
    dict(code="FR", from_label="De", sent_labels=["Envoyé", "Envoye", "Date"],
         original="Message d'origine", verified=False,
         wrote=(r"^\s*Le\b.*\ba [ée]crit\s*:\s*$",
                "Le ven. 7 août 2026 à 20:39, Adam Khalil <adam@acme.com> a écrit :")),
    dict(code="DE", from_label="Von", sent_labels=["Gesendet", "Datum"],
         original="Ursprüngliche Nachricht", verified=False,
         wrote=(r"^\s*Am\b.*\bschrieb\b.*:\s*$",
                "Am Fr., 7. Aug. 2026 um 20:39 Uhr schrieb Adam Khalil <adam@acme.com>:")),
    dict(code="ES", from_label="De", sent_labels=["Enviado", "Fecha"],
         original="Mensaje original", verified=False,
         wrote=(r"^\s*El\b.*\bescribi[óo]\s*:\s*$",
                "El vie, 7 ago 2026 a las 20:39, Adam Khalil (<adam@acme.com>) escribió:")),
    dict(code="IT", from_label="Da", sent_labels=["Inviato", "Data"],
         original="Messaggio originale", verified=False,
         wrote=(r"^\s*Il\b.*\bha scritto\s*:\s*$",
                "Il giorno ven 7 ago 2026 alle ore 20:39 Adam Khalil <adam@acme.com> ha scritto:")),
    dict(code="NL", from_label="Van", sent_labels=["Verzonden", "Datum"],
         original="Oorspronkelijk bericht", verified=False,
         wrote=(r"^\s*Op\b.*\bschreef\b.*:\s*$",
                "Op vr 7 aug. 2026 om 20:39 schreef Adam Khalil <adam@acme.com>:")),
    dict(code="ZH-Hans", from_label="发件人", sent_labels=["发送时间", "日期", "时间"],
         original="原始邮件", verified=True,
         wrote=(r"写道\s*[:：]\s*$",
                "Adam Khalil <adam@acme.com> 于2026年8月7日周五 20:39写道：")),
    dict(code="ZH-Hant", from_label="寄件者", sent_labels=["寄件日期", "傳送時間", "日期", "時間"],
         original="原始郵件", verified=False,
         wrote=(r"寫道\s*[:：]\s*$",
                "Adam Khalil <adam@acme.com> 於 2026年8月7日 週五 下午8:39 寫道：")),
    dict(code="JA", from_label="差出人", sent_labels=["送信日時", "日付", "送信日"],
         original="元のメッセージ", verified=False,
         wrote=(r"のメール\s*[:：]\s*$",
                "2026/08/07 20:39、Adam Khalil <adam@acme.com>のメール:")),
    dict(code="AR", from_label="من", sent_labels=["تاريخ الإرسال", "أرسلت", "التاريخ"],
         original="الرسالة الأصلية", verified=False,
         wrote=(r"^\s*في\b.*\bكتب\b.*:\s*$",
                "في الجمعة، 7 أغسطس 2026، 20:39 كتب Adam Khalil <adam@acme.com>:")),
]

_COLON = r"\s*[:：]"
_EMAIL = r"<[^<>@\s]+@[^<>\s]+>"


def _words(words):
    return "(" + "|".join(re.escape(w) for w in words) + ")"


def language_patterns(lang):
    """Boundary-pattern rows for one LANGUAGES entry."""
    code, note = lang["code"], ("" if lang["verified"] else
                                "Unverified against real mail -- check before trusting.")
    rows = [dict(
        label=f"Outlook header block ({code})",
        line_regex=rf"^\s*{re.escape(lang['from_label'])}{_COLON}\s*\S",
        confirm_regex=rf"^\s*{_words(lang['sent_labels'])}{_COLON}",
        priority=10,
        example_block=(f"{lang['from_label']}: Jane Patel <jane.patel@acme.com>\n"
                       f"{lang['sent_labels'][0]}: 2026-09-03 08:30\n"
                       "RE: vendor onboarding"),
        notes=("Outlook's reply/forward header. The first label alone is common "
               "in prose, so a following sent/date label is required. " + note).strip(),
    )]
    if lang.get("original"):
        rows.append(dict(
            label="Original message marker" + ("" if code == "EN" else f" ({code})"),
            line_regex=rf"^\s*-{{2,}}\s*{re.escape(lang['original'])}\s*-{{2,}}\s*$",
            priority=5,
            example_block=f"----- {lang['original']} -----\n{lang['from_label']}: someone",
            notes=note or None,
        ))
    if lang.get("wrote"):
        regex, example = lang["wrote"]
        rows.append(dict(
            label="Gmail / Apple / mobile On-wrote" + ("" if code == "EN" else f" ({code})"),
            line_regex=regex,
            require_regex=_EMAIL,
            priority=20,
            example_block=example + "\n> earlier text",
            notes=("An address on the same line is required, so ordinary "
                   "sentences ending in 'wrote:' do not match. " + note).strip(),
        ))
    return rows


# Seeded on first init. Each carries a snippet it must cut at line 0.
DEFAULT_PATTERNS = [row for lang in LANGUAGES for row in language_patterns(lang)] + [
    dict(
        label="Forwarded message marker",
        line_regex=r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$",
        priority=5,
        example_block="---------- Forwarded message ----------\nFrom: someone",
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
    """Return compiled enabled patterns and errors; callers decide whether to proceed."""
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
    """Identity of the boundary-pattern set, plus the code that applies it.

    Stored on each message as boundary_patterns_version. Add, edit or disable
    a pattern and the number changes, so every message goes stale and
    re-splits on the next run -- nothing to remember to bump. The code version
    is folded in as well, so a change to the matching logic itself also
    restales every row.
    """
    # JSON preserves field boundaries; include every matching input.
    def regex_key(regex):
        return [regex.pattern, regex.flags] if regex is not None else None

    settings = [
        SPLITTER_CODE_VERSION, textnorm.CODE_VERSION, MAX_JOIN,
        [[p.pattern_id, p.priority, p.confirm_within,
          regex_key(p.line), regex_key(p.require), regex_key(p.confirm)]
         for p in patterns],
    ]
    seed = json.dumps(settings, ensure_ascii=True, separators=(",", ":"))
    return zlib.crc32(seed.encode("utf-8")) & 0x7FFFFFFF


def split(body_text, patterns):
    """Cut a body at its first boundary, keeping only what is above it.

    Returns (unique_body_text, pattern_id). pattern_id is None when no boundary was
    found, in which case the whole body is unique_body_text. The chain below the cut is
    discarded -- body_raw still holds it, so nothing is lost.
    """
    if not body_text:
        return "", None

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
                return textnorm.normalize("\n".join(lines[:i])), p.pattern_id
    return textnorm.normalize(body_text), None


def split_messages(db, rebuild=False, dry_run=False, log=print):
    """Split every message whose result is stale. `body_raw` is never touched.

    Re-splitting changes `unique_body_text`, so the cleaner's output is invalidated too.
    """
    patterns, broken = load_patterns(db)
    for pattern_id, label, err in broken:
        log(f"  ! pattern {pattern_id} ({label}) failed to compile: {err}")
    if broken:
        raise ValueError("Invalid boundary patterns; fix or disable them before splitting.")
    # An empty enabled set is valid: retain the full normalized body.

    if not dry_run:
        textnorm.normalize_messages(db, log=log)
    version = fingerprint(patterns)
    where = "" if rebuild else \
        ("AND (boundary_patterns_version IS NULL OR boundary_patterns_version != :v "
         "OR textnorm_version IS NULL OR textnorm_version != :t)")
    rows = db.execute(
        f"SELECT msg_key, body_raw, body_type, body_text, textnorm_version FROM messages "
        f"WHERE body_raw IS NOT NULL {where}", {"v": version, "t": textnorm.CODE_VERSION}
    ).fetchall()

    if not rows:
        log(f"up to date (version {version}); nothing stale")
        return 0

    unsplit = 0
    for msg_key, body_raw, body_type, cached, text_version in rows:
        text = cached if cached is not None and text_version == textnorm.CODE_VERSION else textnorm.to_text(body_raw, body_type)
        unique_body_text, pattern_id = split(text, patterns)
        if pattern_id is None:
            unsplit += 1
        if dry_run:
            continue
        db.execute(
            "UPDATE messages SET unique_body_text = ?, boundary_pattern_id = ?, "
            "       boundary_patterns_version = ?, cleaner_version = NULL, "
            "       cleaned_unique_body_text = NULL "
            "WHERE msg_key = ?",
            (unique_body_text, pattern_id, version, msg_key),
        )
        db.execute("DELETE FROM disclaimer_hits WHERE msg_key = ?", (msg_key,))
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
        unique_body_text, pattern_id = split(example, [pattern])
        if pattern_id is None:
            log(f"  FAIL {label}: does not match its own example")
            failures += 1
        elif unique_body_text:
            log(f"  FAIL {label}: cuts at the wrong line, left {unique_body_text!r}")
            failures += 1
        else:
            log(f"  ok   {label}")
    log(f"{len(rows) - failures}/{len(rows)} patterns pass")
    return failures
