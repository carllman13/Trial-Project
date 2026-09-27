"""Pull mail from Microsoft Graph (Outlook.com / M365) into the database.

Uses MSAL device-code login + Graph REST. No classic Outlook COM, no pywin32.

    pip install msal requests
    python cli.py mail.db fetch-graph --since-days 30 --limit 20

Token cache is stored next to the DB as `<db>.msal_cache.bin` so you only
sign in once per machine (until the refresh token expires).
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Iterable, Optional

import db as dbmod

# Public client used by Microsoft Graph PowerShell — works for personal
# Outlook.com accounts with delegated Mail.Read. For production apps, replace
# with your own Azure app registration client id.
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["User.Read", "Mail.Read"]
GRAPH = "https://graph.microsoft.com/v1.0"

MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,subject,from,"
    "toRecipients,ccRecipients,bccRecipients,"
    "sentDateTime,receivedDateTime,hasAttachments,body,parentFolderId"
)


def _cache_path(db_path: str) -> Path:
    return Path(db_path).with_suffix(".msal_cache.bin")


def acquire_token(db_path: str, log=print):
    """Interactive device-code login; reuses the on-disk MSAL cache when valid."""
    import msal

    cache = msal.SerializableTokenCache()
    path = _cache_path(db_path)
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))

    app = msal.PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"device flow failed: {flow}")
        log(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            result.get("error_description")
            or result.get("error")
            or "token acquisition failed"
        )
    if cache.has_state_changed:
        path.write_text(cache.serialize(), encoding="utf-8")
    return result["access_token"]


def graph_get(token: str, path: str, params: Optional[dict] = None):
    import requests

    url = path if path.startswith("http") else f"{GRAPH}{path}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Prefer": 'outlook.body-content-type="html"'},
        params=params,
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"Graph {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def whoami(token: str):
    return graph_get(token, "/me")


def _addr(obj) -> tuple[Optional[str], Optional[str]]:
    if not obj:
        return None, None
    ea = obj.get("emailAddress") or {}
    return (ea.get("address") or None), (ea.get("name") or None)


def _iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # Graph returns e.g. 2026-09-24T10:00:00Z already; normalise Z.
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def message_from_graph(item: dict, folder_path: str = "Inbox"):
    """Map one Graph message JSON to (msg_dict, participants) for db.store_message."""
    key = item.get("internetMessageId")
    if not key:
        return None
    sender_addr, sender_name = _addr(item.get("from"))
    body = item.get("body") or {}
    content = body.get("content") or ""
    content_type = (body.get("contentType") or "html").lower()
    body_type = "html" if content_type == "html" else "text"

    participants = []
    if sender_addr:
        participants.append(("from", sender_addr, sender_name))
    dropped = 0
    for role, field in (("to", "toRecipients"),
                        ("cc", "ccRecipients"),
                        ("bcc", "bccRecipients")):
        for recip in item.get(field) or []:
            addr, name = _addr(recip)
            if not addr:
                dropped += 1
                continue
            participants.append((role, addr, name))

    msg = {
        "msg_key": key,
        "conversation_id": item.get("conversationId"),
        "attachment_names": None,
        "subject": item.get("subject"),
        "sender_name": sender_name,
        "sender_addr": sender_addr,
        "sent_time": _iso(item.get("sentDateTime")),
        "received_time": _iso(item.get("receivedDateTime")),
        "folder": folder_path,
        "has_attachments": 1 if item.get("hasAttachments") else 0,
        "recipients_dropped": dropped,
        "body_raw": content,
        "body_type": body_type,
    }
    return msg, participants


def _since_filter(since_days: int) -> Optional[str]:
    if not since_days:
        return None
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days + 1)
    # Graph filter wants UTC without timezone suffix quirks.
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"receivedDateTime ge {stamp}"


def iter_mail_folders(token: str, parent_id: Optional[str] = None,
                      path_prefix: str = ""):
    """Yield (folder_id, display_path) for every mail folder, depth-first."""
    if parent_id is None:
        data = graph_get(
            token, "/me/mailFolders",
            {"$select": "id,displayName,childFolderCount", "$top": 100},
        )
    else:
        data = graph_get(
            token, f"/me/mailFolders/{parent_id}/childFolders",
            {"$select": "id,displayName,childFolderCount", "$top": 100},
        )
    while True:
        for folder in data.get("value") or []:
            name = folder.get("displayName") or folder["id"]
            path = f"{path_prefix}/{name}" if path_prefix else name
            yield folder["id"], path
            if folder.get("childFolderCount"):
                yield from iter_mail_folders(token, folder["id"], path)
        nxt = data.get("@odata.nextLink")
        if not nxt:
            return
        data = graph_get(token, nxt)


def iter_messages(token: str, folder: str = "inbox", since_days: int = 30,
                  limit: Optional[int] = None,
                  since: Optional[_dt.datetime] = None):
    """Yield Graph message dicts (with body) from one folder id or well-known name.

    `since` (an aware datetime) overrides `since_days`.
    """
    params = {
        "$select": MESSAGE_SELECT,
        "$orderby": "receivedDateTime desc",
        "$top": min(limit or 50, 50),
    }
    if since is not None:
        filt = "receivedDateTime ge " + since.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        filt = _since_filter(since_days)
    if filt:
        params["$filter"] = filt

    data = graph_get(token, f"/me/mailFolders/{folder}/messages", params)
    yielded = 0
    while True:
        for item in data.get("value") or []:
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        nxt = data.get("@odata.nextLink")
        if not nxt:
            return
        remaining = None if limit is None else limit - yielded
        if remaining is not None and remaining <= 0:
            return
        data = graph_get(token, nxt)


def _ingest(conn, item, folder_label):
    """One Graph item -> store_message. Returns status string or 'skipped'."""
    key = item.get("internetMessageId")
    if not key:
        return "skipped"
    status = dbmod.update_existing_folder(conn, key, folder_label)
    if status is not None:
        return status
    mapped = message_from_graph(item, folder_path=folder_label)
    if mapped is None:
        return "skipped"
    msg, participants = mapped
    return dbmod.store_message(conn, msg, participants)


def fetch_messages(conn, db_path: str, folder: str = "inbox",
                   since_days: int = 30, limit: Optional[int] = None,
                   recurse: bool = False, log=print, token: Optional[str] = None):
    """Insert new Graph messages; existing msg_key rows only update folder.

    folder: well-known name, Graph folder id, or 'all' for every mail folder.
    recurse: with a single parent folder name/id, also walk its children.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if token is None:
        token = acquire_token(db_path, log=log)
    me = whoami(token)
    log(f"Graph signed in as {me.get('mail') or me.get('userPrincipalName')}")

    if folder in ("all", "*"):
        targets = list(iter_mail_folders(token))
    elif recurse:
        # Resolve display path for the root, then include descendants.
        roots = [(fid, path) for fid, path in iter_mail_folders(token)
                 if fid == folder or path.lower() == folder.lower()
                 or path.split("/")[-1].lower() == folder.lower()]
        if not roots and folder.lower() == "inbox":
            roots = [("inbox", "Inbox")]
        if not roots:
            roots = [(folder, folder)]
        root_id, root_path = roots[0]
        targets = [(root_id, root_path)]
        targets.extend(iter_mail_folders(token, root_id, root_path))
    else:
        label = "Inbox" if folder.lower() == "inbox" else folder
        targets = [(folder, label)]

    stored = moved = unchanged = skipped = failed = 0
    remaining = limit
    for folder_id, folder_label in targets:
        if remaining is not None and remaining <= 0:
            break
        log(f"  scanning {folder_label}")
        count = 0
        try:
            for item in iter_messages(token, folder=folder_id,
                                      since_days=since_days, limit=remaining):
                try:
                    status = _ingest(conn, item, folder_label)
                    if status == "inserted":
                        stored += 1
                    elif status == "moved":
                        moved += 1
                    elif status == "unchanged":
                        unchanged += 1
                    else:
                        skipped += 1
                    count += 1
                    if remaining is not None:
                        remaining -= 1
                        if remaining <= 0:
                            break
                    if (stored + moved + unchanged) % 50 == 0:
                        conn.commit()
                except Exception as exc:
                    failed += 1
                    if failed <= 5:
                        log(f"  ! item in {folder_label} failed: {exc}")
        except Exception as exc:
            log(f"  ! {folder_label}: {exc}")
            if "folder" in str(exc).lower() or "ErrorItemNotFound" in str(exc):
                continue
            failed += 1
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (f"last_fetch:{folder_label}", stamp),
        )
        log(f"  {folder_label}: {count} message(s)")

    conn.commit()
    log(f"stored {stored} new; moved {moved}; unchanged {unchanged}; "
        f"skipped {skipped}; failed {failed}")
    return stored


class GraphSource:
    """Mail source for dashboard_api.install(source=...) on machines without
    classic Outlook. Folder paths match the COM paths, e.g. "收件箱/UKVI"."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._folders = None

    def _token(self, progress):
        def log(text):
            print(text, flush=True)
            progress(text)
        return acquire_token(self.db_path, log=log)

    def folder_tree(self, progress):
        token = self._token(progress)
        progress("Reading folders")
        self._folders = {path: fid for fid, path in iter_mail_folders(token)}
        roots, nodes = [], {}
        for path in self._folders:
            node = nodes[path] = dict(path=path, name=path.rsplit("/", 1)[-1], children=[])
            parent = path.rsplit("/", 1)[0] if "/" in path else None
            (nodes[parent]["children"] if parent in nodes else roots).append(node)
        return roots

    def fetch_folder(self, conn, path, since, progress):
        """Store messages received at or after `since` (None = whole folder)."""
        token = self._token(progress)
        if self._folders is None or path not in self._folders:
            self._folders = {p: fid for fid, p in iter_mail_folders(token)}
        folder_id = self._folders.get(path)
        if folder_id is None:
            raise RuntimeError(f"Folder not found in Outlook: {path}")
        started = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = 0
        for item in iter_messages(token, folder=folder_id, since_days=0, since=since):
            _ingest(conn, item, path)
            count += 1
            if count % 50 == 0:
                conn.commit()
                progress(f"Fetching {path}: {count} message(s)")
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (f"last_fetch:{path}", started),
        )
        conn.commit()
        return count


if __name__ == "__main__":
    sys.exit("run via serve.py (Refresh) or: python cli.py mail.db fetch-graph")
