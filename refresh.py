"""One refresh operation, whatever the button was called.

The UI offers three choices, but they differ only in where scanning starts:

    cutoff (last 24h)      since = now - delta
    since last update      since = each folder's own watermark
    from start             since = None

So there is one function, and the caller decides `since`. A fourth mode later
-- "since a specific date", "last 7 days" -- needs no change here.

Nothing in this module prints. It reports through a `progress` callback and
returns a result object, so a terminal and a UI are equal callers.
"""
import datetime as _dt
import json

import cleaner
import db as dbmod
import folders as folders_mod
import splitter


# --------------------------------------------------------------------------
# Working out where to start
# --------------------------------------------------------------------------

def since_from_cutoff(days=0, hours=0, minutes=0):
    """'Everything newer than 0d 24h 0m'. Returns None if the cutoff is zero,
    which correctly means 'no cutoff'."""
    delta = _dt.timedelta(days=days, hours=hours, minutes=minutes)
    if not delta:
        return None
    return _dt.datetime.now(_dt.timezone.utc) - delta


def since_from_watermarks(conn, paths):
    """Each folder resumes from its own last refresh.

    A folder never scanned gets None -- a full scan -- which is the only
    correct answer for it, and means one new folder does not force a full
    rescan of the others.
    """
    out = {}
    for path in paths:
        row = folders_mod.get(conn, path)
        stamp = row and row["last_refreshed_at"]
        out[path] = _parse(stamp) if stamp else None
    return out


def _parse(stamp):
    return _dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------
# The result the caller renders
# --------------------------------------------------------------------------

class RefreshResult:
    """What happened, in a shape a UI can render and a log can store."""

    def __init__(self):
        self.folders = {}          # path -> {'seen', 'stored', 'moved', 'error'}
        self.started_at = _dt.datetime.now(_dt.timezone.utc)
        self.finished_at = None
        self.split = 0
        self.cleaned = 0

    @property
    def seen(self):
        return sum(f["seen"] for f in self.folders.values())

    @property
    def stored(self):
        return sum(f["stored"] for f in self.folders.values())

    @property
    def moved(self):
        return sum(f["moved"] for f in self.folders.values())

    @property
    def errors(self):
        return {p: f["error"] for p, f in self.folders.items() if f["error"]}

    def summary(self):
        parts = [f"{self.seen} seen", f"{self.stored} new", f"{self.moved} moved"]
        if self.split or self.cleaned:
            parts.append(f"{self.split} split, {self.cleaned} cleaned")
        if self.errors:
            parts.append(f"{len(self.errors)} folder(s) failed")
        return "; ".join(parts)

    def as_json(self):
        return json.dumps({
            "folders": self.folders, "split": self.split,
            "cleaned": self.cleaned,
            "elapsed_s": round((self.finished_at - self.started_at).total_seconds(), 1)
            if self.finished_at else None,
        }, default=str)


# --------------------------------------------------------------------------
# The operation
# --------------------------------------------------------------------------

def refresh(conn, source, paths=None, since=None, process=True,
            limit=None, progress=None):
    """Scan folders and bring the database up to date.

    paths    which folders; defaults to the user's selected set
    since    a datetime for all folders, or {path: datetime}, or None for all
    process  run the splitter and cleaner afterwards
    progress progress(done, total, label) -- for a bar or a log line

    One folder failing does not stop the others; the failure is recorded
    against that folder and the run continues.
    """
    if paths is None:
        paths = [f["path"] for f in folders_mod.selected(conn)]
    if not paths:
        raise ValueError("no folders selected -- tick at least one first")

    per_folder = since if isinstance(since, dict) else {p: since for p in paths}
    result = RefreshResult()
    run_id = _log_start(conn, "refresh")

    try:
        for index, path in enumerate(paths, 1):
            if progress:
                progress(index - 1, len(paths), path)
            stats = {"seen": 0, "stored": 0, "moved": 0, "error": None}
            result.folders[path] = stats
            try:
                known = _known_keys(conn)
                for msg, participants in source.messages(
                        path, since=per_folder.get(path), limit=limit,
                        known=known):
                    outcome = dbmod.store_message(conn, msg, participants)
                    stats["seen"] += 1
                    if outcome == "inserted":
                        stats["stored"] += 1
                    elif outcome == "moved":
                        stats["moved"] += 1
                folders_mod.mark_refreshed(conn, path, message_count=stats["seen"])
                conn.commit()
            except Exception as exc:            # one bad folder must not end the run
                conn.rollback()
                stats["error"] = str(exc)
        if progress:
            progress(len(paths), len(paths), "done")

        if process:
            result.split = splitter.split_messages(conn, log=lambda *a: None)
            result.cleaned = cleaner.clean_messages(conn, log=lambda *a: None)

        result.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _log_finish(conn, run_id, ok=not result.errors,
                    summary=result.summary(), detail=result.as_json())
        return result
    except Exception as exc:
        result.finished_at = _dt.datetime.now(_dt.timezone.utc)
        _log_finish(conn, run_id, ok=False, summary="refresh failed",
                    detail=str(exc))
        raise


# --------------------------------------------------------------------------
# Run log -- so a failure on a machine we cannot see survives as a row
# --------------------------------------------------------------------------

def _known_keys(conn):
    """A membership test the source uses to skip work it does not need to do.

    Loaded once per folder rather than queried per message: a set of a few
    hundred thousand short strings is a few megabytes, and one query beats
    one-per-message by a wide margin.
    """
    keys = {row[0] for row in conn.execute("SELECT msg_key FROM messages")}
    return keys.__contains__


def _log_start(conn, command):
    cur = conn.execute(
        "INSERT INTO run_log (command, started_at) VALUES (?, ?)",
        (command, _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()
    return cur.lastrowid


def _log_finish(conn, run_id, ok, summary, detail=None):
    conn.execute(
        "UPDATE run_log SET finished_at = ?, ok = ?, summary = ?, detail = ? "
        "WHERE run_id = ?",
        (_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         1 if ok else 0, summary, detail, run_id))
    conn.commit()


def recent_runs(conn, limit=10):
    cur = conn.execute(
        "SELECT run_id, command, started_at, finished_at, ok, summary "
        "FROM run_log ORDER BY run_id DESC LIMIT ?", (limit,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
