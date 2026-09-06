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

# --------------------------------------------------------------------------
# fetch -- the COM-facing helpers, exercised with stubs.
#
# None of this can run on a build machine, so the awkward parts (address
# resolution, key extraction, time conversion) are written as plain functions
# over a mail item and tested against objects that behave like Outlook's,
# including the ways Outlook fails: absent properties raise, GetExchangeUser()
# returns None for distribution lists, and internal senders come back as
# directory paths rather than addresses.
# --------------------------------------------------------------------------
print("\nfetch -- Outlook quirks, against stubs")
import datetime as dt

import fetch


class Props:
    """PropertyAccessor: absent tags raise, exactly as Outlook does."""
    def __init__(self, **tags):
        self._tags = tags

    def GetProperty(self, tag):
        if tag not in self._tags:
            raise Exception("property not found")
        return self._tags[tag]


class Obj:
    """Anything with a PropertyAccessor and arbitrary attributes."""
    def __init__(self, props=None, **attrs):
        self.PropertyAccessor = Props(**(props or {}))
        for k, v in attrs.items():
            setattr(self, k, v)


class Exploding:
    """An attribute Outlook refuses to hand over."""
    def __get__(self, *a):
        raise Exception("COM error")


MSG_ID = fetch.PR_INTERNET_MESSAGE_ID
SENDER_SMTP = fetch.PR_SENDER_SMTP_ADDRESS
SMTP = fetch.PR_SMTP_ADDRESS
EX_PATH = "/O=EXCHANGE/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=JSMITH"

check("role 1 is to", fetch.role_for(1), "to")
check("role 2 is cc", fetch.role_for(2), "cc")
check("role 3 is bcc", fetch.role_for(3), "bcc")
check("unknown role is skipped", fetch.role_for(5), None)

check("message key from PR_INTERNET_MESSAGE_ID",
      fetch.message_key(Obj({MSG_ID: "<a1@acme.com>"})), "<a1@acme.com>")
check("no message key means skip the item", fetch.message_key(Obj({})), None)

# Sender: the SMTP property first, then the Exchange lookup, then the raw
# field -- and never a directory path.
check("sender via SMTP property",
      fetch.sender_address(Obj({SENDER_SMTP: "jane.patel@acme.com"})),
      "jane.patel@acme.com")
check("sender via GetExchangeUser when the property is absent",
      fetch.sender_address(Obj(
          {}, Sender=Obj({}, GetExchangeUser=lambda: Obj(
              {}, PrimarySmtpAddress="jane.patel@acme.com")),
          SenderEmailAddress=EX_PATH)),
      "jane.patel@acme.com")
check("external sender falls back to the raw field",
      fetch.sender_address(Obj({}, Sender=None,
                               SenderEmailAddress="ds.ext@blackfuel.ai")),
      "ds.ext@blackfuel.ai")
check("an unresolved directory path is stored as NULL, not as junk",
      fetch.sender_address(Obj({}, Sender=None, SenderEmailAddress=EX_PATH)),
      None)

check("distribution list resolves via GetExchangeDistributionList",
      fetch.smtp_from_address_entry(Obj(
          {}, GetExchangeUser=lambda: None,
          GetExchangeDistributionList=lambda: Obj({}, PrimarySmtpAddress="team@acme.com"),
          Address=EX_PATH)),
      "team@acme.com")
check("address entry with nothing resolvable", fetch.smtp_from_address_entry(
      Obj({}, GetExchangeUser=lambda: None, Address=EX_PATH)), None)
check("no address entry at all", fetch.smtp_from_address_entry(None), None)

print("\nfetch -- directory lookups are cached")
calls = {"n": 0}

def counting_entry(addr, smtp):
    """An AddressEntry that records how often the directory was asked."""
    def lookup():
        calls["n"] += 1
        return Obj({}, PrimarySmtpAddress=smtp)
    return Obj({}, AddressEntryUserType=0, GetExchangeUser=lookup, Address=addr)

cache = {}
for _ in range(5):                       # the same colleague, five messages
    got = fetch.smtp_from_address_entry(counting_entry(EX_PATH, "jane@acme.com"), cache)
check("cached lookup returns the address", got, "jane@acme.com")
check("the directory is asked once, not five times", calls["n"], 1)

calls["n"] = 0
for _ in range(3):
    fetch.smtp_from_address_entry(counting_entry(EX_PATH, "jane@acme.com"))
check("without a cache every call is a round trip", calls["n"], 3)

# Someone who has left will not resolve on a retry either.
calls["n"] = 0
gone = lambda: Obj({}, AddressEntryUserType=0,
                   GetExchangeUser=lambda: None, Address="/O=EX/CN=GONE")
cache = {}
for _ in range(4):
    check_true("departed user stays unresolved",
               fetch.smtp_from_address_entry(gone(), cache) is None)
check("a failed lookup is cached too", len(cache), 1)

print("\nfetch -- entry type decides which lookup runs")
tried = []
dl = Obj({}, AddressEntryUserType=1,
         GetExchangeUser=lambda: tried.append("user"),
         GetExchangeDistributionList=lambda: (tried.append("dl") or
                                              Obj({}, PrimarySmtpAddress="team@acme.com")),
         Address="/O=EX/CN=TEAM")
check("a group resolves via the distribution-list lookup",
      fetch.smtp_from_address_entry(dl), "team@acme.com")
check("the user lookup is not attempted for a group", tried, ["dl"])

plain = Obj({}, AddressEntryUserType=30, Address="ds.ext@blackfuel.ai")
check("a plain SMTP entry needs no directory lookup at all",
      fetch.smtp_from_address_entry(plain), "ds.ext@blackfuel.ai")

print("\nfetch -- times")
aware = dt.datetime(2026, 9, 1, 9, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
check("aware time converts to UTC", fetch.to_utc_iso(aware), "2026-09-01T07:14:00Z")
check("missing time stays None", fetch.to_utc_iso(None), None)
os.environ["TZ"] = "Europe/London"
try:
    import time as _time
    _time.tzset()
    naive = dt.datetime(2026, 9, 1, 9, 14, 0)          # BST, UTC+1
    check("naive time is read as local, not as UTC",
          fetch.to_utc_iso(naive), "2026-09-01T08:14:00Z")
except AttributeError:
    pass                                                # not a POSIX machine

print("\nfetch -- item to row")
item = Obj(
    {MSG_ID: "<x1@acme.com>", SENDER_SMTP: "jane.patel@acme.com"},
    SenderName="Jane Patel", Subject="RE: Q3 numbers", ConversationID="c1",
    SentOn=aware, ReceivedTime=aware, HTMLBody="<p>Hi</p>", Attachments=None,
    Recipients=[
        Obj({SMTP: "carl.wei@acme.com"}, Type=1, Name="Carl Wei",
            AddressEntry=Obj({SMTP: "carl.wei@acme.com"})),
        Obj({}, Type=2, Name="Legal",
            AddressEntry=Obj({}, GetExchangeUser=lambda: Obj(
                {}, PrimarySmtpAddress="legal@acme.com"))),
        Obj({}, Type=2, Name="Unresolvable",
            AddressEntry=Obj({}, GetExchangeUser=lambda: None, Address=EX_PATH)),
    ])
msg, parts = fetch.message_from_item(item, folder_path="\\Mailbox\\Inbox")
check("msg_key", msg["msg_key"], "<x1@acme.com>")
check("sender_addr resolved", msg["sender_addr"], "jane.patel@acme.com")
check("sent_time in UTC", msg["sent_time"], "2026-09-01T07:14:00Z")
check("body kept raw as html", (msg["body_raw"], msg["body_type"]), ("<p>Hi</p>", "html"))
check("folder recorded", msg["folder"], "\\Mailbox\\Inbox")
check("participants include sender and resolved recipients", sorted(parts), sorted([
    ("from", "jane.patel@acme.com", "Jane Patel"),
    ("to", "carl.wei@acme.com", "Carl Wei"),
    ("cc", "legal@acme.com", "Legal"),
]))
check_true("unresolvable recipient is dropped, not stored as a path",
           all(not a.startswith("/") for _r, a, _n in parts))
check("a dropped recipient is counted, not lost quietly",
      msg["recipients_dropped"], 1)

check("an item with no message id is skipped",
      fetch.message_from_item(Obj({}, SenderName="x")), None)

class NoHtml(Obj):
    HTMLBody = Exploding()
check("plain-text item falls back to Body",
      fetch.body_of(NoHtml({}, Body="just text")), ("just text", "text"))

print("\nfetch -- addresses are normalised and gaps are visible")
conn = with_db()
# The same person, as Outlook hands them over on two different messages.
for i, addr in enumerate(("Jane.Patel@Acme.com", "jane.patel@acme.com")):
    dbmod.store_message(conn, {"msg_key": f"<case{i}@x>", "sender_addr": addr,
                               "sender_name": "Patel, Jane", "body_raw": "<p>a</p>"},
                        [("from", addr, "Patel, Jane")])
conn.commit()
check("sender_addr is lowercased, so one person is one row",
      [r[0] for r in conn.execute(
          "SELECT DISTINCT sender_addr FROM messages ORDER BY sender_addr")],
      ["jane.patel@acme.com"])
check("participants agree with messages",
      [r[0] for r in conn.execute(
          "SELECT DISTINCT addr FROM participants WHERE role='from'")],
      ["jane.patel@acme.com"])

dbmod.store_message(conn, {"msg_key": "<gap@x>", "sender_addr": None,
                           "sender_name": "Departed Colleague",
                           "recipients_dropped": 3, "body_raw": "<p>a</p>"}, [])
conn.commit()
total, no_sender, dropped = conn.execute(
    "SELECT COUNT(*), SUM(CASE WHEN sender_addr IS NULL THEN 1 ELSE 0 END), "
    "SUM(recipients_dropped) FROM messages").fetchone()
check("an unresolved sender is counted", no_sender, 1)
check("dropped recipients are counted", dropped, 3)
check("the sender name is still recorded, so the person is identifiable",
      conn.execute("SELECT sender_name FROM messages WHERE sender_addr IS NULL"
                   ).fetchone()[0], "Departed Colleague")

print("\nmigration -- a database from an earlier version gains new columns")
import re as _re, sqlite3 as _sq, tempfile as _tf
_old = os.path.join(_tf.mkdtemp(), "old.db")
_sql = open("schema.sql").read().replace(
    "    recipients_dropped  INTEGER DEFAULT 0,"
    "  -- addresses Outlook would not resolve\n", "")
_c = _sq.connect(_old); _c.executescript(_sql)
_c.execute("INSERT INTO messages (msg_key, sender_addr) VALUES ('<x@y>','a@b.com')")
_c.commit(); _c.close()
_c = dbmod.init(_old, log=lambda *a: None)
check("the new column is added",
      "recipients_dropped" in [r[1] for r in _c.execute("PRAGMA table_info(messages)")],
      True)
check("existing rows survive", _c.execute("SELECT msg_key FROM messages").fetchone()[0],
      "<x@y>")

print("\nfetch -- stored rows survive the pipeline")
conn = with_db()
msg, parts = fetch.message_from_item(item, folder_path="Inbox")
dbmod.store_message(conn, msg, parts)
conn.commit()
check("derived columns start NULL so the splitter picks it up",
      conn.execute("SELECT content, cleaned_content, splitter_version "
                   "FROM messages").fetchone(), (None, None, None))
splitter.split_messages(conn, log=lambda *a: None)
cleaner.clean_messages(conn, log=lambda *a: None)
check("fetched row cleans", conn.execute(
    "SELECT cleaned_content FROM messages").fetchone()[0], "Hi")
check("cc is queryable by address", conn.execute(
    "SELECT COUNT(*) FROM participants WHERE role='cc' AND addr='legal@acme.com'"
).fetchone()[0], 1)

check_true("restrict filter is local-time and locale-formatted",
           "/" in fetch.restrict_filter(dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all tests pass")
