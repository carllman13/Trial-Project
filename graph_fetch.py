"""Pull mail from Outlook.com / Microsoft 365 via Microsoft Graph.

Reuses db.update_existing_folder and db.store_message -- the same contracts
classic Outlook COM fetch.py writes -- so textnorm / split / clean run
unchanged afterwards.

    python3 cli.py mail.db fetch-graph --since-days 30
    python3 cli.py mail.db fetch-graph --check-auth

Requires Graph credentials; see graph_auth.py and README.
Stdlib only (urllib); no pywin32, works on Linux.
"""
from __future__ import annotations

import datetime as _dt
import sys

import db as dbmod
import graph_auth

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
# Keep the payload small; body is large but required for the pipeline.
MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,subject,from,sender,"
    "toRecipients,ccRecipients,bccRecipients,"
    "sentDateTime,receivedDateTime,hasAttachments,body,parentFolderId"
)
# Attachment names only (no content bytes).
ATTACHMENT_SELECT = "id,name,isInline"


def to_utc_iso(value):
    """Graph timestamps are ISO8601; normalise to ...Z like fetch.py."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        return text if "T" in text else None
    try:
        if text.endswith("+00:00"):
            return text[:-6] + "Z"
        stamp = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return stamp.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _addr_of(recipient):
    if not recipient:
        return None, None
    ea = recipient.get("emailAddress") or {}
    addr = (ea.get("address") or "").strip() or None
    name = (ea.get("name") or "").strip() or None
    return addr, name


def body_of(item):
    body = item.get("body") or {}
    content = body.get("content") or ""
    ctype = (body.get("contentType") or "text").lower()
    if ctype == "html":
        return content, "html"
    return content, "text"


def participants_of(item):
    """([(role, addr, name), ...], dropped_count) -- same shape as fetch.py."""
    rows, dropped = [], 0
    from_addr, from_name = _addr_of(item.get("from") or item.get("sender"))
    if from_addr:
        rows.append(("from", from_addr, from_name))
    elif from_name:
        # Display name without SMTP -- count as a gap, like COM unresolved.
        dropped += 1

    for role, key in (("to", "toRecipients"),
                      ("cc", "ccRecipients"),
                      ("bcc", "bccRecipients")):
        for recipient in item.get(key) or []:
            addr, name = _addr_of(recipient)
            if not addr:
                dropped += 1
                continue
            rows.append((role, addr, name))
    return rows, dropped


def message_from_graph(item, folder_path=None, attachment_names=None):
    """One Graph message JSON -> (msg_dict, participants) for db.store_message.

    Returns None when there is no internetMessageId (drafts / odd items).
    """
    key = item.get("internetMessageId")
    if not key:
        return None
    sender_addr, sender_name = _addr_of(item.get("from") or item.get("sender"))
    body, body_type = body_of(item)
    participants, dropped = participants_of(item)
    # If from lacked an address, participants_of already counted the drop;
    # still record display name on the message row.
    names = attachment_names
    if names is None and item.get("attachments"):
        names = "\n".join(
            a["name"] for a in item["attachments"]
            if a.get("name") and not a.get("isInline")
        ) or None

    msg = {
        "msg_key": key,
        "conversation_id": item.get("conversationId"),
        "attachment_names": names,
        "subject": item.get("subject"),
        "sender_name": sender_name,
        "sender_addr": sender_addr,
        "sent_time": to_utc_iso(item.get("sentDateTime")),
        "received_time": to_utc_iso(item.get("receivedDateTime")),
        "folder": folder_path,
        "has_attachments": 1 if item.get("hasAttachments") else 0,
        "recipients_dropped": dropped,
        "body_raw": body,
        "body_type": body_type,
    }
    return msg, participants


def _folder_display_path(folder):
    # Graph well-known names; prefer displayName.
    return folder.get("displayName") or folder.get("id")


def resolve_folder(token, folder_path=None):
    """Return (folder_id, display_path). Default: Inbox well-known folder."""
    if not folder_path or folder_path.lower() in ("inbox", "inbox/"):
        data = graph_auth.graph_get(f"{GRAPH_ROOT}/me/mailFolders/inbox", token)
        return data["id"], _folder_display_path(data) or "Inbox"

    parts = [p for p in folder_path.replace("\\", "/").split("/") if p]
    # Start at root mail folders.
    current_id = None
    display_parts = []
    url = f"{GRAPH_ROOT}/me/mailFolders"
    for part in parts:
        found = None
        next_url = url if current_id is None else \
            f"{GRAPH_ROOT}/me/mailFolders/{current_id}/childFolders"
        for folder in _iter_pages(token, next_url):
            if (folder.get("displayName") or "").lower() == part.lower():
                found = folder
                break
        if found is None:
            raise SystemExit(f"Graph folder not found: {folder_path} "
                             f"(missing segment {part!r})")
        current_id = found["id"]
        display_parts.append(found.get("displayName") or part)
    return current_id, "/".join(display_parts)


def _iter_pages(token, url, extra_params=None):
    params = dict(extra_params or {})
    first = True
    while url:
        if first and params:
            page = graph_auth.graph_get(url, token, params)
            first = False
        else:
            page = graph_auth.graph_get(url, token)
            first = False
        for item in page.get("value") or []:
            yield item
        url = page.get("@odata.nextLink")


def _child_folders(token, folder_id):
    url = f"{GRAPH_ROOT}/me/mailFolders/{folder_id}/childFolders"
    return list(_iter_pages(token, url, {"$top": "100"}))


def _walk_folders(token, folder_id, display_path, recurse):
    yield folder_id, display_path
    if recurse:
        for child in _child_folders(token, folder_id):
            child_path = f"{display_path}/{child.get('displayName') or child['id']}"
            yield from _walk_folders(token, child["id"], child_path, True)


def _attachment_names(token, message_id):
    url = f"{GRAPH_ROOT}/me/messages/{message_id}/attachments"
    names = []
    for att in _iter_pages(token, url, {"$select": ATTACHMENT_SELECT, "$top": "50"}):
        if att.get("name") and not att.get("isInline"):
            names.append(att["name"])
    return "\n".join(names) or None


def _messages_url(folder_id):
    return f"{GRAPH_ROOT}/me/mailFolders/{folder_id}/messages"


def fetch_messages(conn, folder=None, recurse=False, since_days=30,
                   limit=None, log=print, since=None, strict=False,
                   token=None, fetch_attachment_names=True):
    """Insert new Graph mail; existing IDs only need a folder comparison.

    Mirrors fetch.fetch_messages behaviour and writes through db.store_message.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")

    if token is None:
        token = graph_auth.acquire_token(log=log)

    account = (graph_auth._env("MS_GRAPH_ACCOUNT")
               or graph_auth._env("OUTLOOK_ACCOUNT")
               or "carlwei2017@outlook.com")
    graph_auth.verify_token(token, account_hint=account, log=log)

    root_id, root_path = resolve_folder(token, folder)
    if folder:
        root_path = folder  # keep the path the caller asked for

    if since is None and since_days:
        since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days + 1)

    stored = moved = unchanged = processed = skipped = failed = 0
    for folder_id, path in _walk_folders(token, root_id, root_path, recurse):
        scan_started = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "$select": MESSAGE_SELECT,
            "$orderby": "receivedDateTime desc",
            "$top": "50",
        }
        if since:
            # Graph filter uses ISO8601 UTC.
            since_iso = since.astimezone(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            params["$filter"] = f"receivedDateTime ge {since_iso}"

        count = 0
        try:
            url = _messages_url(folder_id)
            for item in _iter_pages(token, url, params):
                if limit is not None and processed >= limit:
                    break
                try:
                    key = item.get("internetMessageId")
                    if not key:
                        skipped += 1
                        continue
                    status = dbmod.update_existing_folder(conn, key, path)
                    if status is not None:
                        moved += status == "moved"
                        unchanged += status == "unchanged"
                        processed += 1
                        count += 1
                        if processed % 200 == 0:
                            conn.commit()
                        continue
                    names = None
                    if fetch_attachment_names and item.get("hasAttachments"):
                        try:
                            names = _attachment_names(token, item["id"])
                        except Exception as exc:
                            if strict:
                                raise
                            log(f"  ! attachments for {key}: {exc}")
                    result = message_from_graph(item, path, attachment_names=names)
                    if result is None:
                        skipped += 1
                        continue
                    msg, participants = result
                    dbmod.store_message(conn, msg, participants)
                    stored += 1
                    processed += 1
                    count += 1
                    if processed % 200 == 0:
                        conn.commit()
                        log(f"  ... {stored} stored")
                except Exception as exc:
                    if strict:
                        raise
                    failed += 1
                    if failed <= 5:
                        log(f"  ! item in {path} failed: {exc}")
        except Exception as exc:
            log(f"  ! {path}: cannot read items: {exc}")
            if strict:
                raise
            continue

        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (f"last_fetch:{path}", scan_started),
        )
        log(f"  {path}: {count} message(s)")
        if limit is not None and processed >= limit:
            break

    conn.commit()
    log(f"stored {stored} new; moved {moved}; unchanged {unchanged}; "
        f"skipped {skipped} keyless; {failed} failed")
    if failed:
        log("  a failed item is usually one Graph would not hand over; "
            "re-running is safe")
    return stored


def check_auth(log=print):
    """Verify credentials and print the signed-in mailbox; no mail is fetched."""
    status = graph_auth.auth_status()
    log("Auth material present:")
    for key, value in status.items():
        log(f"  {key}: {value}")
    token = graph_auth.acquire_token(log=log)
    account = status.get("account_hint") or "carlwei2017@outlook.com"
    me = graph_auth.verify_token(token, account_hint=account, log=log)
    # Touch inbox metadata so we know Mail.Read works.
    inbox = graph_auth.graph_get(f"{GRAPH_ROOT}/me/mailFolders/inbox", token)
    log(f"Inbox reachable ({inbox.get('displayName')}, "
        f"{inbox.get('totalItemCount', '?')} items)")
    return me


if __name__ == "__main__":
    sys.exit("run this through cli.py, e.g.  python3 cli.py mail.db fetch-graph")
