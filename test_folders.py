"""Folder tree and refresh, against a fake mailbox. No Windows, no COM."""
import datetime as dt
import os
import sys
import tempfile

import db as dbmod
import folders
import refresh
import sources

FAIL = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        FAIL.append(name)


TREE = [
    "Mailbox - Inbox",
    "Mailbox - Inbox - Clients",
    "Mailbox - Inbox - Clients - Acme",
    "Mailbox - Inbox - Internal",
    "Mailbox - Sent",
]


def msg(key, folder, when, body="<p>Hi</p>"):
    return ({"msg_key": key, "folder": folder, "body_raw": body,
             "sender_addr": "jane@acme.com", "sender_name": "Jane",
             "received_time": when, "sent_time": when},
            [("from", "jane@acme.com", "Jane")])


MAIL = {
    "Mailbox - Inbox": [msg("<a@x>", "Mailbox - Inbox", "2026-09-01T09:00:00Z"),
                        msg("<b@x>", "Mailbox - Inbox", "2026-09-06T09:00:00Z")],
    "Mailbox - Inbox - Clients": [msg("<c@x>", "Mailbox - Inbox - Clients",
                                      "2026-09-05T09:00:00Z")],
    "Mailbox - Sent": [msg("<d@x>", "Mailbox - Sent", "2026-09-04T09:00:00Z")],
}


def fresh():
    conn = dbmod.init(os.path.join(tempfile.mkdtemp(), "t.db"), log=lambda *a: None)
    return conn, sources.FakeSource(TREE, MAIL)


print("folder tree")
conn, src = fresh()
n = folders.load_tree(conn, src)
check("every folder is stored", n, 5 + 1)          # 5 leaves + the store root
check("store root has no parent",
      [f["path"] for f in folders.children_of(conn, None)], ["Mailbox"])
check("children are queryable without touching the source",
      [f["path"] for f in folders.children_of(conn, "Mailbox - Inbox")],
      ["Mailbox - Inbox - Clients", "Mailbox - Inbox - Internal"])
check("has_children is recorded for the UI arrow",
      folders.get(conn, "Mailbox - Inbox - Clients")["has_children"], 1)

print("\nselection")
check("nothing is selected to begin with", folders.selected(conn), [])
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
check("recursive selection picks up descendants",
      [f["path"] for f in folders.selected(conn)],
      ["Mailbox - Inbox", "Mailbox - Inbox - Clients",
       "Mailbox - Inbox - Clients - Acme", "Mailbox - Inbox - Internal"])
check("a sibling stays unselected",
      folders.get(conn, "Mailbox - Sent")["selected"], 0)
folders.set_selected(conn, ["Mailbox - Inbox - Internal"], selected=False)
check("one folder can be unticked",
      folders.get(conn, "Mailbox - Inbox - Internal")["selected"], 0)

print("\nreloading the tree preserves the user's choices")
folders.mark_refreshed(conn, "Mailbox - Inbox", when="2026-09-05T15:46:00Z")
conn.commit()
folders.load_tree(conn, src)
check("selection survives a tree reload",
      folders.get(conn, "Mailbox - Inbox")["selected"], 1)
check("watermark survives a tree reload",
      folders.get(conn, "Mailbox - Inbox")["last_refreshed_at"],
      "2026-09-05T15:46:00Z")

print("\nrefresh -- from start")
conn, src = fresh()
folders.load_tree(conn, src)
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
r = refresh.refresh(conn, src, since=None)
check("all mail in the selected subtree is stored", r.stored, 3)
check("unselected folders are not scanned",
      conn.execute("SELECT COUNT(*) FROM messages WHERE folder='Mailbox - Sent'"
                   ).fetchone()[0], 0)
check("the pipeline ran", (r.split, r.cleaned) != (0, 0), True)
check("no errors", r.errors, {})

print("\nrefresh -- a second run stores nothing new")
r2 = refresh.refresh(conn, src, since=None)
check("nothing re-stored", r2.stored, 0)
check("everything was still seen", r2.seen, 3)

print("\nrefresh -- cutoff")
conn, src = fresh()
folders.load_tree(conn, src)
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
cut = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)
r = refresh.refresh(conn, src, since=cut)
check("only mail newer than the cutoff is stored", r.stored, 2)

print("\nrefresh -- since last update, per folder")
conn, src = fresh()
folders.load_tree(conn, src)
folders.set_selected(conn, ["Mailbox - Inbox", "Mailbox - Inbox - Clients"])
folders.mark_refreshed(conn, "Mailbox - Inbox", when="2026-09-06T00:00:00Z")
conn.commit()
paths = [f["path"] for f in folders.selected(conn)]
per = refresh.since_from_watermarks(conn, paths)
check("a folder never scanned resumes from the beginning",
      per["Mailbox - Inbox - Clients"], None)
r = refresh.refresh(conn, src, paths=paths, since=per)
check("the watched folder only picks up what is newer than its watermark",
      r.folders["Mailbox - Inbox"]["stored"], 1)
check("the unscanned folder gets everything",
      r.folders["Mailbox - Inbox - Clients"]["stored"], 1)

print("\nfolder moves")
conn, src = fresh()
folders.load_tree(conn, src)
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
refresh.refresh(conn, src, since=None)
before = conn.execute("SELECT folder, content FROM messages WHERE msg_key='<a@x>'"
                      ).fetchone()
moved = dict(MAIL)
moved["Mailbox - Inbox - Clients"] = MAIL["Mailbox - Inbox - Clients"] + [
    msg("<a@x>", "Mailbox - Inbox - Clients", "2026-09-01T09:00:00Z")]
r = refresh.refresh(conn, sources.FakeSource(TREE, moved), since=None)
after = conn.execute("SELECT folder, content FROM messages WHERE msg_key='<a@x>'"
                     ).fetchone()
check("a moved message updates its folder", (before[0], after[0]),
      ("Mailbox - Inbox", "Mailbox - Inbox - Clients"))
check("its processed content is untouched", after[1], before[1])
check("it is counted as moved, not new", (r.moved, r.stored), (1, 0))
check("still one row", conn.execute(
    "SELECT COUNT(*) FROM messages WHERE msg_key='<a@x>'").fetchone()[0], 1)

print("\na cutoff hides old messages entirely, moves included")
conn, src = fresh()
folders.load_tree(conn, src)
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
refresh.refresh(conn, src, since=None)
moved_old = dict(MAIL)
moved_old["Mailbox - Inbox - Clients"] = MAIL["Mailbox - Inbox - Clients"] + [
    msg("<a@x>", "Mailbox - Inbox - Clients", "2026-09-01T09:00:00Z")]
cut = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)
r = refresh.refresh(conn, sources.FakeSource(TREE, moved_old), since=cut)
check("an old message that moved is not seen past the cutoff", r.moved, 0)
check("so its folder is unchanged", conn.execute(
    "SELECT folder FROM messages WHERE msg_key='<a@x>'").fetchone()[0],
    "Mailbox - Inbox")

print("\nknown messages are not re-extracted")
class Counting(sources.FakeSource):
    """Records how often the expensive path was taken."""
    extracted = 0
    def messages(self, path, since=None, limit=None, known=None):
        for msg, parts in super().messages(path, since, limit, known):
            if parts:                      # full extraction, not the light dict
                Counting.extracted += 1
            yield msg, parts

conn, _ = fresh()
src = Counting(TREE, MAIL)
folders.load_tree(conn, src)
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
Counting.extracted = 0
refresh.refresh(conn, src, since=None)
check("first scan extracts every message", Counting.extracted, 3)
Counting.extracted = 0
r = refresh.refresh(conn, src, since=None)
check("a rescan extracts nothing", Counting.extracted, 0)
check("but every message is still seen", r.seen, 3)

print("\ncutoff helpers")
check("a zero cutoff means no cutoff", refresh.since_from_cutoff(0, 0, 0), None)
check("24h cutoff is a time", refresh.since_from_cutoff(hours=24) is not None, True)
_c, _s = fresh()
folders.load_tree(_c, _s)
folders.set_selected(_c, ["Mailbox - Inbox", "Mailbox - Sent"])
folders.mark_refreshed(_c, "Mailbox - Inbox", when="2026-09-05T15:46:00Z")
_c.commit()
check("earliest refresh is None while any selected folder is unscanned",
      folders.earliest_refresh(_c), None)
folders.mark_refreshed(_c, "Mailbox - Sent", when="2026-09-06T10:00:00Z")
_c.commit()
check("once all are scanned it is the oldest of them",
      folders.earliest_refresh(_c), "2026-09-05T15:46:00Z")

print("\nrun log")
runs = refresh.recent_runs(conn)
check("every refresh is recorded", len(runs) >= 2, True)
check("the newest run succeeded", runs[0]["ok"], 1)
check("it carries a one-line summary", bool(runs[0]["summary"]), True)

print("\nfailure isolation")
class Broken(sources.FakeSource):
    def messages(self, path, since=None, limit=None, known=None):
        if path == "Mailbox - Inbox - Clients":
            raise RuntimeError("Outlook said no")
        return super().messages(path, since, limit, known)

conn, _ = fresh()
folders.load_tree(conn, sources.FakeSource(TREE, MAIL))
folders.set_selected(conn, ["Mailbox - Inbox"], recursive=True)
r = refresh.refresh(conn, Broken(TREE, MAIL), since=None)
check("the failing folder is recorded", list(r.errors), ["Mailbox - Inbox - Clients"])
check("the other folders still stored their mail", r.stored, 2)
check("the run is logged as failed", refresh.recent_runs(conn)[0]["ok"], 0)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {', '.join(FAIL)}")
    sys.exit(1)
print("all tests pass")
