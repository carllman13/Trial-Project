"""Microsoft Graph authentication for Outlook.com / Microsoft 365 mail.

No secrets are stored in the repo. Provide credentials via environment
variables (or an interactive device-code flow once a public client id is set).

Preferred for a personal Outlook.com mailbox (e.g. carlwei2017@outlook.com):

    # Option A -- reuse a token you already have (CI / one-shot):
    export MS_GRAPH_ACCESS_TOKEN='eyJ...'

    # Option B -- device-code OAuth (interactive; needs an Azure app registration
    # registered as a public client, with redirect for native/device code):
    export MS_GRAPH_CLIENT_ID='xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
    export MS_GRAPH_TENANT='consumers'   # personal MSA; or 'common'
    # optional: save refresh token for later non-interactive runs
    export MS_GRAPH_TOKEN_CACHE=./.graph_token_cache.json

    python3 cli.py mail.db fetch-graph --since-days 7

Also accepted:

    MS_GRAPH_REFRESH_TOKEN   refresh an access token (needs CLIENT_ID;
                             CLIENT_SECRET only if the app is confidential)
    MS_GRAPH_CLIENT_SECRET   confidential-client apps only
    MS_GRAPH_TOKEN_FILE      path to a file whose entire contents is the access token
    MS_GRAPH_SCOPES          space-separated scopes (default: Mail.Read User.Read offline_access)

Client-credentials (app-only) is intentionally not the primary path: it does
not work for personal Outlook.com mailboxes. Work/school app-only would need
Mail.Read application permission plus admin consent and a user id; use a
delegated token for this account instead.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SCOPES = "https://graph.microsoft.com/Mail.Read offline_access User.Read"
DEFAULT_TENANT = "consumers"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
DEVICE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"


class AuthError(SystemExit):
    """Raised/exited when credentials are missing or rejected."""


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def auth_status():
    """Describe what credential material is present (no secret values)."""
    flags = {
        "access_token": bool(_env("MS_GRAPH_ACCESS_TOKEN") or _token_file_contents()),
        "refresh_token": bool(_env("MS_GRAPH_REFRESH_TOKEN")),
        "client_id": bool(_env("MS_GRAPH_CLIENT_ID")),
        "client_secret": bool(_env("MS_GRAPH_CLIENT_SECRET")),
        "token_cache": bool(_env("MS_GRAPH_TOKEN_CACHE") and
                            os.path.isfile(_env("MS_GRAPH_TOKEN_CACHE"))),
        "tenant": _env("MS_GRAPH_TENANT", DEFAULT_TENANT),
        "account_hint": _env("MS_GRAPH_ACCOUNT") or _env("OUTLOOK_ACCOUNT"),
    }
    return flags


def missing_auth_message():
    return (
        "Microsoft Graph auth is not configured.\n"
        "\n"
        "Provide one of:\n"
        "  1. MS_GRAPH_ACCESS_TOKEN          (or MS_GRAPH_TOKEN_FILE)\n"
        "  2. MS_GRAPH_CLIENT_ID             (+ interactive device-code login)\n"
        "  3. MS_GRAPH_REFRESH_TOKEN         (+ MS_GRAPH_CLIENT_ID)\n"
        "\n"
        "For Outlook.com personal mail use tenant 'consumers' (default):\n"
        "  export MS_GRAPH_TENANT=consumers\n"
        "  export MS_GRAPH_ACCOUNT=carlwei2017@outlook.com\n"
        "\n"
        "Register a public-client Azure AD app, enable 'Allow public client\n"
        "flows', and add Microsoft Graph delegated permission Mail.Read.\n"
        "See README (Outlook.com / Graph fetch)."
    )


def _token_file_contents():
    path = _env("MS_GRAPH_TOKEN_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError as exc:
        raise AuthError(f"MS_GRAPH_TOKEN_FILE unreadable ({path}): {exc}") from exc


def _scopes():
    return _env("MS_GRAPH_SCOPES", DEFAULT_SCOPES)


def _tenant():
    return _env("MS_GRAPH_TENANT", DEFAULT_TENANT)


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AuthError(f"OAuth token request failed ({exc.code}): {detail}") from exc


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path, payload):
    # Never log the payload; keep file private if possible.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _refresh(refresh_token, client_id, client_secret=None):
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": _scopes(),
    }
    if client_secret:
        data["client_secret"] = client_secret
    return _post_form(TOKEN_URL.format(tenant=_tenant()), data)


def _device_code_login(client_id, log=print):
    start = _post_form(
        DEVICE_URL.format(tenant=_tenant()),
        {"client_id": client_id, "scope": _scopes()},
    )
    log(start.get("message") or (
        f"To sign in, visit {start['verification_uri']} and enter code "
        f"{start['user_code']}"
    ))
    account = _env("MS_GRAPH_ACCOUNT") or _env("OUTLOOK_ACCOUNT")
    if account:
        log(f"Sign in as {account} when prompted.")
    interval = int(start.get("interval", 5))
    expires = time.time() + int(start.get("expires_in", 900))
    while time.time() < expires:
        time.sleep(interval)
        try:
            return _post_form(
                TOKEN_URL.format(tenant=_tenant()),
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client_id,
                    "device_code": start["device_code"],
                },
            )
        except AuthError as exc:
            text = str(exc)
            if "authorization_pending" in text:
                continue
            if "slow_down" in text:
                interval += 2
                continue
            raise
    raise AuthError("Device-code login timed out before authorization completed.")


def acquire_token(log=print, interactive=True):
    """Return a bearer access token, or raise AuthError with setup instructions."""
    token = _env("MS_GRAPH_ACCESS_TOKEN") or _token_file_contents()
    if token:
        return token

    client_id = _env("MS_GRAPH_CLIENT_ID")
    client_secret = _env("MS_GRAPH_CLIENT_SECRET")
    cache_path = _env("MS_GRAPH_TOKEN_CACHE")
    refresh = _env("MS_GRAPH_REFRESH_TOKEN")

    if cache_path and os.path.isfile(cache_path):
        cached = _load_cache(cache_path)
        refresh = refresh or cached.get("refresh_token")
        # Prefer a still-fresh access token from cache if present.
        expires_at = cached.get("expires_at", 0)
        if cached.get("access_token") and expires_at > time.time() + 60:
            return cached["access_token"]

    if refresh and client_id:
        payload = _refresh(refresh, client_id, client_secret)
        if cache_path:
            _cache_tokens(cache_path, payload)
        return payload["access_token"]

    if client_id and interactive:
        # Device code needs a TTY for the user to complete the flow.
        if not sys.stdin.isatty() and not _env("MS_GRAPH_ALLOW_DEVICE_CODE"):
            raise AuthError(
                missing_auth_message()
                + "\n\n(Device-code flow skipped: no TTY. Set "
                  "MS_GRAPH_ACCESS_TOKEN or MS_GRAPH_ALLOW_DEVICE_CODE=1.)"
            )
        payload = _device_code_login(client_id, log=log)
        if cache_path:
            _cache_tokens(cache_path, payload)
        elif payload.get("refresh_token"):
            log("Tip: set MS_GRAPH_TOKEN_CACHE to persist the refresh token "
                "for later non-interactive runs.")
        return payload["access_token"]

    raise AuthError(missing_auth_message())


def _cache_tokens(path, payload):
    expires_at = time.time() + int(payload.get("expires_in", 3600)) - 30
    existing = _load_cache(path) if os.path.isfile(path) else {}
    existing.update({
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token") or existing.get("refresh_token"),
        "expires_at": expires_at,
        "token_type": payload.get("token_type", "Bearer"),
    })
    _save_cache(path, existing)


def graph_get(url, token, params=None):
    """GET a Graph URL with the bearer token; return parsed JSON."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AuthError(f"Graph request failed ({exc.code}) for {url}: {detail}") from exc


def verify_token(token, account_hint=None, log=print):
    """Call /me and optionally check the signed-in mailbox matches the hint."""
    me = graph_get(GRAPH_ME, token)
    mail = (me.get("mail") or me.get("userPrincipalName") or "").lower()
    log(f"Graph connected as {me.get('displayName') or '(no name)'} <{mail}>")
    if account_hint and mail and account_hint.lower() not in (mail,):
        # Soft warning: aliases / UPN vs SMTP can differ.
        log(f"  note: expected account hint was {account_hint}; "
            f"signed-in identity is {mail}")
    return me
