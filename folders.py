"""The folder tree: stored in SQLite, selected by the user, scanned on refresh.

The tree lives in the `folders` table rather than a side file, so there is one
source of truth and no chance of the two drifting.

Three things happen here, and none of them touch Outlook directly -- they all
go through a Source:

    load_tree()     walk Outlook to a depth and store what is found
    expand()        load one folder's children on demand (a UI arrow-click)
    selection       which folders refresh will scan, remembered between runs

Everything else in this file is a pure query over the stored tree.
"""
import datetime as _dt

SEP = " - "
# How deep the eager walk goes. Deeper than this is loaded on demand, because
# a full walk of a large mailbox is slow and most of it is never opened.
DEFAULT_MAX_DEPTH = 7


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Writing the tree
# --------------------------------------------------------------------------

def _upsert(conn, node, children_loaded=False):
    """Store one folder without disturbing the user's selection or watermark.

    A refresh of the tree must never silently untick a folder or forget when it
    was last scanned, so those two columns are left alone on an existing row.
    """
    conn.execute(
        "INSERT INTO folders (path, name, parent_path, entry_id, store_id, "
        "                     has_children, children_loaded, seen_at) "
        "VALUES (:path, :name, :parent_path, :entry_id, :store_id, "
        "        :has_children, :children_loaded, :seen_at) "
        "ON CONFLICT(path) DO UPDATE SET "
        "  name = excluded.name, parent_path = excluded.parent_path, "
        "  entry_id = excluded.entry_id, store_id = excluded.store_id, "
        "  has_children = excluded.has_children, "
        "  children_loaded = MAX(folders.children_loaded, excluded.children_loaded), "
        "  seen_at = excluded.seen_at",
        {"path": node["path"], "name": node["name"],
         "parent_path": node.get("parent_path"),
         "entry_id": node.get("entry_id"), "store_id": node.get("store_id"),
         "has_children": 1 if node.get("has_children") else 0,
         "children_loaded": 1 if children_loaded else 0,
         "seen_at": _now()},
    )


def load_tree(conn, source, max_depth=DEFAULT_MAX_DEPTH, progress=None):
    """Walk the mail system to `max_depth` and store the folders found.

    Returns the number of folders seen. Folders that have disappeared from
    Outlook are left in the table with an old `seen_at` rather than deleted --
    they may still own messages, and deleting them would orphan those rows.
    """
    seen = 0
    frontier = [(node, 0) for node in source.stores()]
    while frontier:
        node, depth = frontier.pop(0)
        deeper = depth + 1 <= max_depth and node.get("has_children")
        _upsert(conn, node, children_loaded=bool(deeper))
        seen += 1
        if progress:
            progress(seen, None, node["path"])
        if deeper:
            for child in source.children(node["path"]):
                frontier.append((child, depth + 1))
    conn.commit()
    return seen


def expand(conn, source, path):
    """Load one folder's children on demand. Returns the child rows.

    This is the UI arrow-click. Already-expanded folders are served from the
    table without touching Outlook at all.
    """
    row = conn.execute(
        "SELECT children_loaded FROM folders WHERE path = ?", (path,)).fetchone()
    if row and row[0]:
        return children_of(conn, path)
    for child in source.children(path):
        _upsert(conn, child)
    conn.execute("UPDATE folders SET children_loaded = 1 WHERE path = ?", (path,))
    conn.commit()
    return children_of(conn, path)


# --------------------------------------------------------------------------
# Reading the tree -- pure, no Source, no COM
# --------------------------------------------------------------------------

def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def children_of(conn, parent_path=None):
    """Direct children of one folder, or the store roots when parent is None."""
    if parent_path is None:
        return _rows(conn, "SELECT * FROM folders WHERE parent_path IS NULL "
                           "ORDER BY name")
    return _rows(conn, "SELECT * FROM folders WHERE parent_path = ? ORDER BY name",
                 (parent_path,))


def get(conn, path):
    rows = _rows(conn, "SELECT * FROM folders WHERE path = ?", (path,))
    return rows[0] if rows else None


def descendants(conn, path, include_self=True):
    """Every folder at or below `path`, from the stored tree.

    This is what turns a user's "watch this folder and everything under it"
    into the flat list refresh() actually scans. No COM, no recursion into
    Outlook -- purely a prefix match over what is already known.
    """
    out = _rows(conn, "SELECT * FROM folders WHERE path = ? OR path LIKE ? "
                      "ORDER BY path", (path, path + SEP + "%"))
    return out if include_self else [f for f in out if f["path"] != path]


def set_selected(conn, paths, selected=True, recursive=False):
    """Tick or untick folders. Unselected folders are never scanned."""
    targets = set()
    for path in paths:
        if recursive:
            targets.update(f["path"] for f in descendants(conn, path))
        else:
            targets.add(path)
    conn.executemany("UPDATE folders SET selected = ? WHERE path = ?",
                     [(1 if selected else 0, p) for p in sorted(targets)])
    conn.commit()
    return len(targets)


def selected(conn):
    """The folders refresh will scan."""
    return _rows(conn, "SELECT * FROM folders WHERE selected = 1 ORDER BY path")


def mark_refreshed(conn, path, when=None, message_count=None):
    """Move one folder's watermark. Called by refresh() as each folder finishes,
    so an interrupted run keeps the progress of the folders that completed."""
    conn.execute(
        "UPDATE folders SET last_refreshed_at = ?, "
        "       message_count = COALESCE(?, message_count) WHERE path = ?",
        (when or _now(), message_count, path))


def earliest_refresh(conn, paths=None):
    """The oldest watermark among the given folders -- the UI's
    'Earliest refresh among selected'. None if any has never been scanned,
    because that is the honest answer: a full scan is needed."""
    rows = selected(conn) if paths is None else \
        [get(conn, p) for p in paths]
    rows = [r for r in rows if r]
    if not rows:
        return None
    if any(not r["last_refreshed_at"] for r in rows):
        return None
    return min(r["last_refreshed_at"] for r in rows)
