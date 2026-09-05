"""Assertions over the splitter and cleaner. Run: python3 tests.py

Marker samples are copied from real emails.
"""
import os
import sqlite3
import sys
import tempfile

import cleaner
import db as dbmod
import splitter
import textnorm

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        FAILURES.append(name)


def check_true(name, cond, detail=""):
    check(name, bool(cond) or detail, True)


def with_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    return dbmod.init(path, log=lambda *a: None)


# --------------------------------------------------------------------------
print("splitter -- real marker formats")
conn = with_db()
pats, broken = splitter.load_patterns(conn)
check("all seeded patterns compile", broken, [])

REAL_MARKERS = [
    ("gs.com, comma in name",
     "On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:",
     "adam.khalil@gs.com"),
    ("blackfuel, AM/PM date",
     "On Sun, Aug 9, 2026 at 8:49 PM David Salfati <ds.ext@blackfuel.ai> wrote:",
     "ds.ext@blackfuel.ai"),
    ("humain, credentials + timezone",
     "On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain CFA, CAIA "
     "<BAlhussain@humain.com> wrote:",
     "balhussain@humain.com"),
]
for label, marker, want_addr in REAL_MARKERS:
    body = f"My new reply here.\n\n{marker}\n> older text"
    content, hist, pid, addr = splitter.split(body, pats)
    check(f"{label}: content", content, "My new reply here.")
    check(f"{label}: address", addr, want_addr)
    check_true(f"{label}: history kept", hist.startswith("On "))

print("\nsplitter -- wrapping")
# The HTML-to-text converter wraps long markers. Matching line-by-line misses.
wrapped = ("Many thanks Carl, that's very helpful.\n\n"
           "On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain CFA,\n"
           "CAIA <BAlhussain@humain.com> wrote:\nolder text")
content, _h, pid, _a = splitter.split(wrapped, pats)
check("wrapped marker across 2 lines", content, "Many thanks Carl, that's very helpful.")

# A fixed 3-line join would break this one: the window would run past `wrote:`
# into the body text and the end-anchor would fail.
unwrapped = ("Noted.\n"
             "On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:\n"
             "> old stuff")
content, _h, _p, _a = splitter.split(unwrapped, pats)
check("unwrapped marker followed by text", content, "Noted.")

print("\nsplitter -- Outlook's own header block")
outlook = ("Approving now.\n\n"
           "From: Jane Patel <jane.patel@acme.com>\n"
           "Sent: 03 September 2026 08:30\n"
           "To: Carl Wei\n"
           "Subject: RE: vendor onboarding\n\nolder")
content, _h, pid, _a = splitter.split(outlook, pats)
check("From:/Sent: block", content, "Approving now.")

separator = ("Approving now.\n\n"
             "________________________________\n"
             "From: Jane Patel <jane.patel@acme.com>\n"
             "Sent: 03 September 2026 08:30\n\nolder")
content, hist, _p, _a = splitter.split(separator, pats)
# Everything above the boundary stays in content, Outlook's rule line included.
check("cut at the header block, rule line left above it", content,
      "Approving now.\n\n________________________________")
check_true("history starts at the header block", hist.startswith("From:"))

for label, body in [
    ("French De:/Envoyé:", "Bonjour.\n\nDe : Jane <j@acme.com>\nEnvoyé : 3 septembre 2026\nObjet : test\n\nvieux"),
    ("German Von:/Gesendet:", "Hallo.\n\nVon: Jane <j@acme.com>\nGesendet: 3. September 2026\nBetreff: test\n\nalt"),
    ("Original Message", "Hi.\n\n----- Original Message -----\nFrom: someone\n\nold"),
    ("Forwarded message", "Hi.\n\n---------- Forwarded message ----------\nFrom: someone\n\nold"),
]:
    content, _h, pid, _a = splitter.split(body, pats)
    check_true(f"{label} cuts", pid is not None)

print("\nsplitter -- must NOT cut")
for label, body in [
    ("'wrote:' in prose", "On the topic of the memo he wrote:\nI think it is fine."),
    ("'From:' in prose", "From: what I can tell the numbers are fine.\nNo quoting here."),
    ("plain message", "Canteen at 12:30?"),
    ("quoted lines alone", "> some quoted text\n> more quoted text"),
    ("single > in prose", "Use x > y as the filter.\nThat is all."),
]:
    content, hist, pid, _a = splitter.split(body, pats)
    check(f"{label}: no boundary", pid, None)
    check(f"{label}: body intact", content, textnorm.normalize(body))

print("\ncleaner -- one pattern, many wrappings")
matcher = cleaner.build_matcher("Disclaimer: testing xxx.", "literal")
pat = [(1, "test", matcher)]
for label, text in [
    ("as typed", "Thanks, Jane\n\nDisclaimer: testing xxx."),
    ("wrapped mid-phrase", "Thanks, Jane\n\nDisclaimer: testing\nxxx."),
    ("double spaced", "Thanks, Jane\n\nDisclaimer:  testing   xxx."),
    ("non-breaking spaces", "Thanks, Jane\n\nDisclaimer: testing xxx."),
    ("upper case", "Thanks, Jane\n\nDISCLAIMER: TESTING XXX."),
    ("tab separated", "Thanks, Jane\n\nDisclaimer:\ttesting\txxx."),
]:
    out, hits = cleaner.clean_text(textnorm.normalize(text), pat)
    check(f"{label}", out, "Thanks, Jane")

out, hits = cleaner.clean_text("Thanks, Jane\n\nSee attached.", pat)
check("absent pattern leaves text alone", out, "Thanks, Jane\n\nSee attached.")
check("absent pattern records no hit", hits, [])

print("\npipeline -- end to end on the demo mailbox")
conn = with_db()
import demo
demo.load(conn, log=lambda *a: None)
splitter.split_messages(conn, log=lambda *a: None)
cleaner.clean_messages(conn, log=lambda *a: None)

rows = dict(conn.execute("SELECT msg_key, cleaned_content FROM messages"))
check("outlook block: chain and disclaimer gone", rows["<a1@acme.com>"],
      "Hi Carl,\n\nNumbers look fine to me. Approving now.\n\nJane"
      "\n\n________________________________")
check("gmail marker: chain and wrapped disclaimer gone", rows["<b2@gs.com>"],
      "Thanks David - great to connect earlier. We are working on the comps and will revert.")
check("wrapped marker + mobile signature gone", rows["<c3@humain.com>"],
      "Sounds good, looping in Carl.")
check("no chain, nbsp disclaimer gone", rows["<d4@acme.com>"], "Attached.")
check("nothing to do", rows["<e5@acme.com>"], "Canteen at 12:30?")

print("\npipeline -- staleness")
check("second split is a no-op", splitter.split_messages(conn, log=lambda *a: None), 0)
check("second clean is a no-op", cleaner.clean_messages(conn, log=lambda *a: None), 0)

cleaner.add_pattern(conn, "New footer", "Please consider the environment")
check("adding a disclaimer restales every message",
      cleaner.clean_messages(conn, log=lambda *a: None), 5)

# Re-splitting rewrites content, so the cleaner's output must be invalidated.
splitter.split_messages(conn, rebuild=True, log=lambda *a: None)
stale = conn.execute(
    "SELECT COUNT(*) FROM messages WHERE cleaner_version IS NULL").fetchone()[0]
check("re-splitting invalidates cleaning", stale, 5)

print("\npattern self-test")
check("every pattern matches its own example",
      splitter.test_patterns(conn, log=lambda *a: None), 0)

conn.execute("INSERT INTO boundary_patterns (label, line_regex, example_block) "
             "VALUES ('bad', '^\\s*NEVERMATCHESANYTHING', 'On x wrote:')")
check("a pattern that misses its example is caught",
      splitter.test_patterns(conn, log=lambda *a: None), 1)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all tests pass")
