# Work-server merge

Your work `app.py` remains the only server entry point.

## Files to copy or merge

Keep the work versions of `app.py`, `com_thread.py`, and `jobs.py`. Copy or merge
the changed backend files and these new files:

- `read_api.py` — database reads and chain construction.
- `dashboard_api.py` — routes, local-only safeguards, and serialized write jobs.
- `frontend/adapter.js` — browser-to-server connector.
- `test_dashboard.py` — offline regression checks.

## Two edits to the work server

Add an import with the existing imports:

```python
import dashboard_api
```

After existing API routes and before `app.mount(...)`, add:

```python
dashboard_api.install(app, DB_PATH, TREE_CACHE, PERSONAL_STORE)
```

Either copy all `frontend/` files into the existing `static/` directory, or set:

```python
STATIC_DIR = BASE_DIR / "frontend"
```

Keep the static mount at `/`. The new routes live only under
`/api/dashboard/...`, so they do not replace the existing `/api/messages`,
`/api/refresh`, or `/api/jobs` routes.

Start locally only:

```powershell
uvicorn app:app --host 127.0.0.1 --port 8000
```

Do not bind this dashboard to `0.0.0.0`, enable a remote tunnel, or add CORS.
It rejects non-local clients, cross-origin writes, and write requests without the
dashboard browser header. Use one Uvicorn worker and do not use `--reload` for a
real refresh or backfill; an intentional server restart ends in-memory job status.

## Behaviour

| UI action | Result |
| --- | --- |
| Database query | Reads metadata only; bodies load after selection. |
| Folders tab | Groups stored messages by Outlook `conversation_id`. |
| Copy chain | Orders stored messages oldest to newest and joins individual bodies. |
| Save disclaimers | Saves rules only. |
| Start backfilling | Saves submitted rules, then processes stored messages. |
| Refresh | Uses Classic Outlook, then text-normalizes and splits; cleans only when saved auto-clean is on. |

Messages without a `conversation_id` deliberately form one-message chains. If
processing is stale or missing, the chain shows a short pending marker rather
than raw quoted email text.

## First work-computer check

No Outlook access is required for this first check:

```powershell
python -X utf8 -m unittest -q test_dashboard test_processing test_refresh
```

Then run the server and open `http://127.0.0.1:8000/?preview=1` to check the
sample interface. Remove `?preview=1` to use the database connection. Do not
share database files or real email content in any error report.

## Migration

`db.init()` adds missing columns and renames old `content` / `cleaned_content`
columns to `unique_body_text` / `cleaned_unique_body_text`. It never alters
`body_raw`. Cached text is rebuilt locally on the next split/run. Existing
disclaimer rules are preserved, including an intentionally empty list.
