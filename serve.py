"""Minimal local server for this checkout.

Serves frontend/ and mounts dashboard_api against mail.db. Works unchanged
on a machine with classic Outlook (Refresh uses COM) and on one with only
New Outlook (Refresh uses Microsoft Graph).

    python serve.py              start the app and open it in the browser
    python serve.py --shortcut   put an "email app 3" icon on the Desktop

Optional environment variables:
    OUTLOOK_STORE   Outlook mailbox (store) name to show and read folders from;
                    default: the mailbox that holds your default Inbox
    OUTLOOK_DB      database path; default: mail.db next to this file
"""
import os
from pathlib import Path
import socket
import sys
import threading
import webbrowser
import winreg

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

import dashboard_api
import db
import fetch_graph

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("OUTLOOK_DB") or ROOT / "mail.db")
STATIC_DIR = ROOT / "frontend"
URL = "http://127.0.0.1:8000/"
SHORTCUT_NAME = "email app 3"


def classic_outlook_installed():
    """New Outlook (olk.exe) registers no COM class, so Dispatch would fail."""
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Outlook.Application\CLSID"))
        return True
    except OSError:
        return False


# Graph is the temporary stand-in; with classic Outlook, Refresh uses COM.
SOURCE = None if classic_outlook_installed() else fetch_graph.GraphSource(DB_PATH)
ACCOUNT = os.environ.get("OUTLOOK_STORE") or (None if SOURCE is None else "____________.com")

# Creates a missing database and seeds new splitter languages; never touches body_raw.
db.init(str(DB_PATH), log=lambda *a: None).close()

app = FastAPI()
dashboard_api.install(app, DB_PATH, store_name=ACCOUNT, source=SOURCE)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

def create_shortcut():
    """Desktop icon that starts this server with the Python running now."""
    from win32com.client import Dispatch
    shell = Dispatch("WScript.Shell")
    # SpecialFolders follows OneDrive/Group Policy Desktop redirection.
    path = Path(shell.SpecialFolders("Desktop")) / f"{SHORTCUT_NAME}.lnk"
    link = shell.CreateShortcut(str(path))
    link.TargetPath = sys.executable
    link.Arguments = f'"{Path(__file__).resolve()}"'
    link.WorkingDirectory = str(ROOT)
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE")
        link.IconLocation = winreg.QueryValue(key, None) + ",0"
    except OSError:
        pass                         # no classic Outlook: keep the Python icon
    link.Description = "Start the local email app and open it in the browser"
    link.Save()
    print(f"Created {path}")


def already_running():
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", 8000)) == 0


if __name__ == "__main__":
    if "--shortcut" in sys.argv:
        create_shortcut()
        sys.exit()
    if already_running():
        # A second click on the icon just reopens the page.
        webbrowser.open(URL)
        sys.exit()
    print(f"DB: {DB_PATH}")
    print("Refresh uses: " + ("classic Outlook (COM)" if SOURCE is None else "Microsoft Graph (sign-in code appears on first Refresh)"))
    print(f"Open {URL}   (closing this window stops the app)")
    print(f"Synthetic sample: {URL}?preview=1")
    threading.Timer(1.5, webbrowser.open, [URL]).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
