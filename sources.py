"""The boundary between this app and Outlook.

Everything above this line works with plain dicts. Only one implementation of
`Source` touches COM (outlook_com.ComSource), and it imports win32com lazily,
so the whole pipeline -- folders, refresh, splitting, cleaning, tests -- runs
on any machine.

A Source answers four questions and nothing else:

    stores()                     which top-level mailboxes exist
    children(path)               the folders directly under one folder
    messages(path, since)        message dicts in a folder, newest first
    display_name()              who we are connected as (for the UI)

`messages()` yields the dict `db.store_message` expects, plus a
`participants` key. Keeping the shape here rather than in the caller is what
lets a fake stand in for Outlook exactly.
"""


class Source:
    """What refresh.py is allowed to assume about the mail system."""

    def display_name(self):
        raise NotImplementedError

    def stores(self):
        """[{'name', 'path', 'entry_id', 'store_id', 'has_children'}]"""
        raise NotImplementedError

    def children(self, path):
        """Direct children of one folder. Same shape as stores(), plus
        'parent_path'. Never recurses -- the caller decides how deep to go."""
        raise NotImplementedError

    def messages(self, path, since=None, limit=None, known=None):
        """Yield (message_dict, participants) for one folder.

        `since` is an aware UTC datetime or None for everything. Yielding
        rather than returning a list matters: a mailbox does not fit in memory
        twice, and the caller wants to report progress as it goes.

        `known(msg_key) -> bool` lets a source skip the expensive part. Reading
        a message id is one cheap property; pulling the body and resolving
        every address is many round trips. On a full scan almost everything is
        already stored, so a source that honours this yields a minimal dict
        -- msg_key and folder only -- and the caller updates the location
        without touching anything else.
        """
        raise NotImplementedError


class FakeSource(Source):
    """An in-memory mailbox, so every layer above can be tested off Windows.

    Built from a flat dict of path -> list of (message, participants), plus
    the folder tree implied by the paths.
    """

    SEP = " - "

    def __init__(self, folders, messages=None, name="Fake Mailbox"):
        self._name = name
        self._folders = list(folders)          # full paths, e.g. "Mailbox - Inbox - Clients"
        self._messages = messages or {}

    def display_name(self):
        return self._name

    def _nodes_under(self, parent):
        """Immediate children of `parent` (None means top level)."""
        depth = 0 if parent is None else parent.count(self.SEP) + 1
        seen = {}
        for path in self._folders:
            parts = path.split(self.SEP)
            if len(parts) <= depth:
                continue
            candidate = self.SEP.join(parts[: depth + 1])
            if parent is not None and not candidate.startswith(parent + self.SEP):
                continue
            if parent is None and self.SEP in candidate:
                continue
            seen[candidate] = parts[depth]
        out = []
        for path, name in sorted(seen.items()):
            out.append({
                "name": name,
                "path": path,
                "parent_path": parent,
                "entry_id": "eid:" + path,
                "store_id": "sid:" + path.split(self.SEP)[0],
                "has_children": any(
                    p.startswith(path + self.SEP) for p in self._folders),
            })
        return out

    def stores(self):
        return self._nodes_under(None)

    def children(self, path):
        return self._nodes_under(path)

    def messages(self, path, since=None, limit=None, known=None):
        sent = 0
        for msg, participants in self._messages.get(path, []):
            if since is not None and msg.get("received_time"):
                if msg["received_time"] < since.strftime("%Y-%m-%dT%H:%M:%SZ"):
                    continue
            if limit is not None and sent >= limit:
                return
            sent += 1
            if known and known(msg["msg_key"]):
                yield {"msg_key": msg["msg_key"], "folder": path}, ()
            else:
                yield dict(msg, folder=path), participants
