"""The only file that talks to Outlook.

Everything else in this project works with plain dicts and can run anywhere.
win32com is imported lazily inside one function, so importing this module on a
Mac or in CI is harmless -- only calling ComSource() fails.

What it needs, all four:

    Windows                     these are COM methods on Outlook's object model
    Outlook installed           no Outlook, no object model
    a live, logged-in profile   resolving an address is a directory lookup,
                                not a read of the message
    reach to the same directory  or a synced offline address book -- this is
                                what the /O=EXCHANGE pointers point at

Programmatic address access can raise a security prompt or be blocked by group
policy on a managed machine, so test on the work laptop -- a clean run at home
proves nothing.

Two structural rules, both load-bearing:

    One COM thread.  COM objects belong to the apartment that made them. A web
        or desktop UI serves requests on many threads, so every call is
        marshalled onto a single worker thread here. Without this you get
        intermittent, unreproducible failures under a UI and none from a CLI.

    Never walk .Parent.  A folder's path is threaded downwards as the tree is
        built. Asking a folder for its parent is slow and, for search folders
        and delegate stores, sometimes wrong.

Folder identity is cached as (EntryID, StoreID) so a later scan jumps straight
to a folder via GetFolderFromID instead of walking from the root. Those ids
change when a folder is moved or renamed, so a failed lookup falls back to
walking by path and re-caches -- otherwise the tree silently breaks the first
time you reorganise your mailbox.
"""
import datetime as _dt
import queue
import threading

import sources

# MAPI property tags, used through PropertyAccessor.
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
PR_SMTP_ADDRESS        = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"

OL_MAIL_ITEM   = 43   # item.Class for a real email; skip everything else
OL_FOLDER_INBOX = 6

# AddressEntry.AddressEntryUserType. Branching on this beats calling both
# lookups and catching the throw: each attempt is a directory round trip, and
# an exception costs more than a comparison.
OL_EXCHANGE_USER = 0
OL_EXCHANGE_DL   = 1

# Recipient.Type
_ROLES = {1: "to", 2: "cc", 3: "bcc"}


# --------------------------------------------------------------------------
# Helpers -- no COM calls of their own, so these are unit-testable
# --------------------------------------------------------------------------

def role_for(recipient_type):
    """Outlook numbers recipient roles: 1=to, 2=cc, 3=bcc."""
    return _ROLES.get(recipient_type)


def to_utc_iso(value):
    """Outlook times to ISO8601 UTC.

    pywin32 hands back a datetime that may be timezone-aware or naive local
    depending on version. A naive value is assumed to be local time; getting
    this wrong shifts every timestamp by the UTC offset, and by an extra hour
    across a DST boundary.
    """
    if value is None:
        return None
    try:
        stamp = _dt.datetime(value.year, value.month, value.day,
                             value.hour, value.minute, value.second,
                             tzinfo=value.tzinfo)
    except AttributeError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()               # interpret as local
    return stamp.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prop(obj, tag):
    """Read one MAPI property, or None. Absent properties raise, so this is
    always the guarded form."""
    try:
        value = obj.PropertyAccessor.GetProperty(tag)
    except Exception:
        return None
    return value or None


def smtp_from_address_entry(entry, cache=None):
    """Resolve an AddressEntry to a real SMTP address.

    Exchange hands back a directory path rather than an address:

        /O=EXCHANGE/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=JSMITH

    Several routes, because each fails on a different kind of recipient: the
    SMTP property is absent on some entries, GetExchangeUser() returns None
    for a group, and neither exists for external addresses.

    `cache` maps directory path -> result for the run. Every lookup is a round
    trip to the directory, and the same colleagues recur on message after
    message, so without it a large mailbox takes hours. Failures are cached
    too: someone who has left the company will not resolve on the retry
    either.
    """
    if entry is None:
        return None
    try:
        key = entry.Address
    except Exception:
        key = None
    if cache is not None and key in cache:
        return cache[key]

    addr = _resolve_address_entry(entry)
    if cache is not None and key:
        cache[key] = addr
    return addr


def _resolve_address_entry(entry):
    addr = _prop(entry, PR_SMTP_ADDRESS)
    if addr:
        return addr

    # Ask the entry what it is rather than trying both lookups blindly.
    try:
        kind = entry.AddressEntryUserType
    except Exception:
        kind = None
    if kind == OL_EXCHANGE_USER:
        getters = ("GetExchangeUser",)
    elif kind == OL_EXCHANGE_DL:
        getters = ("GetExchangeDistributionList",)
    elif kind is None:
        getters = ("GetExchangeUser", "GetExchangeDistributionList")
    else:
        getters = ()          # contact, LDAP, plain SMTP: nothing to look up

    for getter in getters:
        try:
            resolved = getattr(entry, getter)()
            if resolved is not None:
                addr = resolved.PrimarySmtpAddress
                if addr:
                    return addr
        except Exception:
            pass
    try:
        addr = entry.Address
    except Exception:
        return None
    # A directory path is not an address; better to store nothing than junk.
    return None if addr and addr.startswith("/") else (addr or None)


def sender_address(item, cache=None):
    """The sender's SMTP address, resolved the same way."""
    addr = _prop(item, PR_SENDER_SMTP_ADDRESS)
    if addr:
        return addr
    try:
        if item.Sender is not None:
            addr = smtp_from_address_entry(item.Sender, cache)
            if addr:
                return addr
    except Exception:
        pass
    try:
        raw = item.SenderEmailAddress
    except Exception:
        return None
    return None if raw and raw.startswith("/") else (raw or None)


def message_key(item):
    """The internet message id -- the only identifier stable across folders.

    EntryID changes when a message is moved, so a message filed into a
    subfolder would be stored twice under it.
    """
    return _prop(item, PR_INTERNET_MESSAGE_ID)


def body_of(item):
    """(body, body_type). Plain-text-only messages have no HTMLBody."""
    try:
        html = item.HTMLBody
        if html:
            return html, "html"
    except Exception:
        pass
    try:
        return item.Body or "", "text"
    except Exception:
        return "", "text"


def participants_of(item, sender_name, sender_addr, cache=None):
    """([(role, addr, name), ...], dropped_count).

    A recipient whose address will not resolve is skipped rather than stored
    as an /O=EXCHANGE directory path -- but it is counted, so the gap shows up
    in `report` instead of disappearing quietly.
    """
    rows, dropped = [], 0
    if sender_addr:
        rows.append(("from", sender_addr, sender_name))
    try:
        recipients = list(item.Recipients)
    except Exception:
        return rows, dropped
    for recipient in recipients:
        try:
            role = role_for(recipient.Type)
            if role is None:
                continue
            try:
                entry = recipient.AddressEntry
            except Exception:
                entry = None
            addr = smtp_from_address_entry(entry, cache)
            if not addr:
                addr = _prop(recipient, PR_SMTP_ADDRESS)
            if not addr:
                dropped += 1
                continue
            rows.append((role, addr, getattr(recipient, "Name", None)))
        except Exception:
            dropped += 1
            continue
    return rows, dropped


def message_from_item(item, folder_path=None, cache=None):
    """One Outlook item to the dict db.store_message expects, or None to skip.

    Returns (message_dict, participants) so the caller can store both.
    """
    key = message_key(item)
    if not key:
        return None                       # drafts and some calendar items
    addr = sender_address(item, cache)
    try:
        name = item.SenderName
    except Exception:
        name = None
    body, body_type = body_of(item)
    participants, dropped = participants_of(item, name, addr, cache)

    def attr(field, default=None):
        try:
            return getattr(item, field)
        except Exception:
            return default

    msg = {
        "msg_key": key,
        "conversation_id": attr("ConversationID"),
        "subject": attr("Subject"),
        "sender_name": name,
        "sender_addr": addr,
        "sent_time": to_utc_iso(attr("SentOn")),
        "received_time": to_utc_iso(attr("ReceivedTime")),
        "folder": folder_path,
        "has_attachments": 1 if (attr("Attachments") and item.Attachments.Count) else 0,
        "recipients_dropped": dropped,
        "body_raw": body,
        "body_type": body_type,
    }
    return msg, participants


def restrict_filter(since):
    """Outlook's Restrict filter for messages at or after `since`.

    The date must be a locale-formatted local-time string, not ISO -- Outlook
    parses it with the machine's regional settings. On a machine set to a
    non-US format this can silently match nothing, which is why --no-restrict
    exists.
    """
    local = since.astimezone()
    return "[ReceivedTime] >= '{}'".format(local.strftime("%m/%d/%Y %I:%M %p"))


# --------------------------------------------------------------------------
# One COM thread
# --------------------------------------------------------------------------

class _ComThread:
    """Runs every COM call on one thread, so a multi-threaded UI is safe.

    A call that hangs -- Outlook showing a modal dialog, a directory lookup
    waiting on a dead connection -- would otherwise wedge the whole app, so
    each submission has a timeout.
    """

    def __init__(self, timeout=30):
        self.timeout = timeout
        self._jobs = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def _run(self):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                fn, box = job
                try:
                    box.append(("ok", fn()))
                except BaseException as exc:      # carried to the caller intact
                    box.append(("error", exc))
        finally:
            pythoncom.CoUninitialize()

    def submit(self, fn, timeout=None):
        if not self._started:
            self._thread.start()
            self._started = True
        box, done = [], threading.Event()

        def wrapped():
            try:
                return fn()
            finally:
                done.set()

        self._jobs.put((wrapped, box))
        if not done.wait(timeout or self.timeout):
            raise TimeoutError(
                "Outlook did not answer within "
                f"{timeout or self.timeout}s -- it may be showing a dialog")
        kind, value = box[0]
        if kind == "error":
            raise value
        return value


# --------------------------------------------------------------------------
# The Outlook source
# --------------------------------------------------------------------------

SEP = " - "


class ComSource(sources.Source):
    """Reads the folder tree and messages from a running classic Outlook.

    allowed_stores, when given, limits the walk to those top-level mailboxes.
    Without it a delegate mailbox or Public Folders can turn a tree walk into
    a very long one.
    """

    def __init__(self, allowed_stores=None, timeout=30):
        self._allowed = set(allowed_stores) if allowed_stores else None
        self._com = _ComThread(timeout)
        self._ids = {}                    # path -> (entry_id, store_id)
        self._ns = None

    # -- connection --------------------------------------------------------

    def _namespace(self):
        if self._ns is None:
            self._ns = self._com.submit(self._connect)
        return self._ns

    @staticmethod
    def _connect():
        try:
            import win32com.client
        except ImportError:
            raise RuntimeError(
                "pywin32 is not installed. Run:  pip install pywin32\n"
                "This only works on Windows with classic Outlook running.")
        return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")

    def display_name(self):
        def work():
            try:
                return self._namespace().CurrentUser.Name
            except Exception:
                return "Outlook"
        return self._com.submit(work)

    def seed_cache(self, rows):
        """Prime the id cache from the folders table after a restart, so the
        first scan does not have to walk the tree again."""
        for row in rows:
            if row.get("entry_id") and row.get("store_id"):
                self._ids[row["path"]] = (row["entry_id"], row["store_id"])

    # -- folder identity ---------------------------------------------------

    def _node(self, folder, path, parent_path):
        """One folder as a plain dict. has_children uses Folders.Count, which
        is cheap, rather than materialising the children."""
        try:
            count = folder.Folders.Count
        except Exception:
            count = 0
        entry_id = getattr(folder, "EntryID", None)
        store_id = None
        try:
            store_id = folder.StoreID
        except Exception:
            pass
        if entry_id and store_id:
            self._ids[path] = (entry_id, store_id)
        return {"name": folder.Name, "path": path, "parent_path": parent_path,
                "entry_id": entry_id, "store_id": store_id,
                "has_children": bool(count)}

    def _resolve(self, path):
        """The COM folder for a path.

        Tries the cached ids first -- one call instead of a walk. Falls back to
        walking by name when that fails, which is what happens after a folder
        is moved or renamed and its EntryID changes.
        """
        ids = self._ids.get(path)
        if ids:
            try:
                folder = self._namespace().GetFolderFromID(ids[0], ids[1])
                if folder is not None:
                    return folder
            except Exception:
                self._ids.pop(path, None)     # stale; fall through to the walk
        parts = path.split(SEP)
        current = None
        for store in self._namespace().Folders:
            if store.Name == parts[0]:
                current = store
                break
        if current is None:
            raise LookupError(f"no such store: {parts[0]!r}")
        for part in parts[1:]:
            current = current.Folders[part]
        self._node(current, path, SEP.join(parts[:-1]) if len(parts) > 1 else None)
        return current

    # -- Source interface --------------------------------------------------

    def stores(self):
        def work():
            out = []
            for store in self._namespace().Folders:
                if self._allowed and store.Name not in self._allowed:
                    continue
                out.append(self._node(store, store.Name, None))
            return out
        return self._com.submit(work)

    def children(self, path):
        def work():
            parent = self._resolve(path)
            return [self._node(child, path + SEP + child.Name, path)
                    for child in parent.Folders]
        return self._com.submit(work)

    def messages(self, path, since=None, limit=None, known=None):
        """Yield (message, participants) for one folder, newest first.

        Restrict asks Outlook to filter, which is far faster than reading every
        item -- but its date filter wants a locale-formatted local-time string,
        so on unusual regional settings it can match nothing. When `since` is
        set and Restrict returns nothing, this falls back to filtering here.
        """
        cache = {}
        items = self._com.submit(lambda: self._items(path, since))
        count = 0
        while True:
            batch = self._com.submit(
                lambda: self._read_batch(items, path, cache, 50, known))
            if not batch:
                return
            for row in batch:
                if row is None:
                    continue
                msg, participants = row
                if since and msg.get("received_time"):
                    if msg["received_time"] < since.strftime("%Y-%m-%dT%H:%M:%SZ"):
                        continue
                if limit is not None and count >= limit:
                    return
                count += 1
                yield msg, participants

    def _items(self, path, since):
        folder = self._resolve(path)
        items = folder.Items
        items.Sort("[ReceivedTime]", True)
        if since is not None:
            try:
                restricted = items.Restrict(restrict_filter(since))
                if restricted.Count:
                    return _Cursor(restricted)
            except Exception:
                pass                       # bad locale format; filter in Python
        return _Cursor(items)

    def _read_batch(self, cursor, path, cache, size, known=None):
        """Read a handful of items per hop onto the COM thread.

        One hop per message would be correct but slow; one hop for the whole
        mailbox would block a UI for minutes. A batch is the compromise.

        The message id is read first because it is one cheap property. If the
        message is already stored, nothing else is read at all -- no body, no
        address lookups. On a full rescan that is the difference between
        minutes and hours.
        """
        out = []
        for _ in range(size):
            item = cursor.next()
            if item is None:
                break
            try:
                if item.Class != OL_MAIL_ITEM:
                    continue
                key = message_key(item)
                if not key:
                    continue
                if known and known(key):
                    out.append(({"msg_key": key, "folder": path}, ()))
                    continue
                out.append(message_from_item(item, path, cache))
            except Exception:
                continue                   # one unreadable item is not fatal
        return out


class _Cursor:
    """Outlook's collections are 1-based and do not survive a plain iterator
    across threads, so step through by index."""

    def __init__(self, items):
        self._items = items
        self._i = 0

    def next(self):
        self._i += 1
        try:
            return self._items.Item(self._i)
        except Exception:
            return None
