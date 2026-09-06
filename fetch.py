"""Pull mail from classic Outlook on Windows into the database.

Uses the Outlook desktop application's automation interface via pywin32.

What it needs, all four:

    Windows                     these are COM methods on Outlook's object model
    Outlook installed           no Outlook, no object model
    a live, logged-in profile   resolving an address is a directory lookup,
                                not a read of the message
    reach to the same directory  or a synced offline address book -- this is
                                what the /O=EXCHANGE pointers point at

So it cannot run on a Linux box, in a container, or on any machine without
Outlook. Nothing needs approving by IT, but note that programmatic access to
address information can raise a security prompt, or be blocked outright by
group policy on a managed machine. Test on the work laptop, not a personal
one -- a clean run at home proves nothing about the managed build.

    pip install pywin32
    python3 cli.py mail.db fetch --since-days 30

Only RAW columns are written. content / quoted_history / cleaned_content are
left NULL, which is exactly what marks a message as needing work -- the
splitter and cleaner pick it up on their next run.

The COM-facing parts are thin wrappers; the awkward logic (address resolution,
time conversion, key extraction) lives in small helpers that take a mail item
and can be tested with a stub, since none of this can run on a build machine.
"""
import datetime as _dt
import sys

import db as dbmod

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
# COM walk
# --------------------------------------------------------------------------

def _outlook_namespace():
    """Connect to the running Outlook. Imported here so this module still
    imports on a machine without pywin32."""
    try:
        import win32com.client
    except ImportError:
        raise SystemExit(
            "pywin32 is not installed. Run:  pip install pywin32\n"
            "This only works on Windows with classic Outlook running."
        )
    return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")


def _walk_folders(folder, recurse):
    yield folder
    if recurse:
        for child in folder.Folders:
            for nested in _walk_folders(child, True):
                yield nested


def _resolve_folder(namespace, path):
    """Find a folder by display path, e.g. 'Inbox/Clients'."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    current = namespace.GetDefaultFolder(OL_FOLDER_INBOX)
    if parts and parts[0].lower() == current.Name.lower():
        parts = parts[1:]
    for part in parts:
        current = current.Folders[part]
    return current


def fetch_messages(conn, folder=None, recurse=False, since_days=30,
                   use_restrict=True, limit=None, log=print):
    """Walk Outlook and store every mail item found.

    since_days windows the scan; the window is widened by a day on each run
    because Restrict compares in local time and messages arriving during a run
    would otherwise be missed. Duplicates cost nothing -- msg_key is the
    primary key.
    """
    namespace = _outlook_namespace()
    root = _resolve_folder(namespace, folder) if folder \
        else namespace.GetDefaultFolder(OL_FOLDER_INBOX)

    since = None
    if since_days:
        since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days + 1)

    cache = {}                      # directory path -> SMTP address, per run
    stored = skipped = failed = 0
    for current in _walk_folders(root, recurse):
        try:
            path = current.FolderPath
        except Exception:
            path = current.Name
        try:
            items = current.Items
            items.Sort("[ReceivedTime]", True)
            if since and use_restrict:
                items = items.Restrict(restrict_filter(since))
        except Exception as exc:
            log(f"  ! {path}: cannot read items: {exc}")
            continue

        count = 0
        for item in items:
            if limit is not None and stored >= limit:
                break
            try:
                if item.Class != OL_MAIL_ITEM:
                    skipped += 1
                    continue
                result = message_from_item(item, path, cache)
                if result is None:
                    skipped += 1
                    continue
                msg, participants = result
                if since and not use_restrict:
                    # Restrict was skipped, so filter here instead.
                    if msg["received_time"] and msg["received_time"] < \
                            since.strftime("%Y-%m-%dT%H:%M:%SZ"):
                        continue
                dbmod.store_message(conn, msg, participants)
                stored += 1
                count += 1
                if stored % 200 == 0:
                    conn.commit()          # so a crash does not lose the lot
                    log(f"  ... {stored} stored")
            except Exception as exc:
                failed += 1
                if failed <= 5:
                    log(f"  ! item in {path} failed: {exc}")
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (f"last_fetch:{path}",
             _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        log(f"  {path}: {count} message(s)")

    conn.commit()
    log(f"stored {stored}; skipped {skipped} non-mail or keyless; {failed} failed")
    if cache:
        unresolved = sum(1 for v in cache.values() if not v)
        log(f"  {len(cache)} distinct address(es) looked up, {unresolved} unresolved")
    if failed:
        log("  a failed item is usually one Outlook will not hand over; "
            "re-running is safe")
    return stored


if __name__ == "__main__":
    sys.exit("run this through cli.py, e.g.  python3 cli.py mail.db fetch")
