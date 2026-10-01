"""Local development harness for the dashboard, for use on Linux/containers.

The real deployment mounts ``dashboard_api`` onto the work ``app.py`` (see
``WORK_SERVER_MERGE.md``); that server is not part of this repository. This file
reproduces only the two documented additions -- install the dashboard routes and
serve the ``frontend/`` static files -- so the dashboard can be exercised end to
end without Outlook. It binds to loopback only, exactly like the work server.

    python3 cli.py mail.db init && python3 cli.py mail.db demo && python3 cli.py mail.db run
    uvicorn dev_server:app --host 127.0.0.1 --port 8000

Open http://127.0.0.1:8000/?preview=1 for the synthetic preview, or
http://127.0.0.1:8000/ to read from the SQLite database (DB_PATH below).
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import dashboard_api

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("MAIL_DB", str(BASE_DIR / "mail.db"))
STATIC_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Local Outlook Digest (dev harness)")

dashboard_api.install(app, DB_PATH)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="frontend")
