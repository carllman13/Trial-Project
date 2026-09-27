"""FastAPI wiring. One worker owns each write operation and its SQLite/COM objects.

Install on an existing app before its static mount; do not run two servers.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import List, Optional, Union
import ipaddress
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import db
import fetch
import read_api
import splitter
import cleaner


class Filters(BaseModel):
    folder: str = ''
    folders: List[str] = Field(default_factory=list, max_length=1000)
    fromDate: str = ''
    toDate: str = ''
    days: str = ''
    subject: str = ''
    body: str = ''
    sender: Union[List[str], str] = Field('', alias='from')
    to: Union[List[str], str] = ''
    cc: Union[List[str], str] = ''
    includeCC: bool = False
    evenings: bool = False
    limit: int = Field(500, ge=1, le=5000)


class Rule(BaseModel):
    id: Optional[int] = None
    label: str = ''
    text: str = Field(max_length=100000)
    kind: str = 'literal'
    enabled: bool = True


class Settings(BaseModel):
    disclaimers: List[Rule]
    autoClean: bool = False


class Preferences(BaseModel):
    folderOrder: Optional[List[str]] = Field(None, max_length=10000)
    refreshChecked: Optional[List[str]] = Field(None, max_length=10000)


class Cutoff(BaseModel):
    days: int = Field(0, ge=0, le=36500)
    hours: int = Field(0, ge=0, le=876000)
    minutes: int = Field(0, ge=0, le=52560000)


class Refresh(BaseModel):
    mode: str
    folders: List[str]
    cutoff: Cutoff = Field(default_factory=Cutoff)


def save_settings(conn, settings):
    """Replace the submitted rule list atomically; preserve rule IDs and kinds."""
    if len(settings.disclaimers) > 1000:
        raise ValueError('At most 1000 disclaimer rules are allowed')
    existing = {r[0] for r in conn.execute('SELECT pattern_id FROM disclaimer_patterns')}
    ids = [r.id for r in settings.disclaimers if r.id is not None]
    if len(ids) != len(set(ids)) or not set(ids) <= existing:
        raise ValueError('Unknown or duplicate rule ID; reload the disclaimer list')
    for rule in settings.disclaimers:
        if rule.kind not in ('literal', 'regex'):
            raise ValueError('Unknown disclaimer kind')
        if rule.enabled:
            cleaner.build_matcher(rule.text, rule.kind)
    with conn:
        for ident in existing - set(ids):
            conn.execute('DELETE FROM disclaimer_hits WHERE pattern_id=?', (ident,))
            conn.execute('DELETE FROM disclaimer_patterns WHERE pattern_id=?', (ident,))
        # Temporary unique labels permit label swaps within a single save.
        for ident in ids:
            conn.execute('UPDATE disclaimer_patterns SET label=? WHERE pattern_id=?', (str(uuid.uuid4()), ident))
        for rule in settings.disclaimers:
            label = rule.label.strip() or f'Disclaimer {rule.id or uuid.uuid4().hex[:12]}'
            if rule.id is None:
                conn.execute("INSERT INTO disclaimer_patterns (label,pattern_text,kind,enabled,created_at,updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))", (label, rule.text, rule.kind, rule.enabled))
            else:
                conn.execute("UPDATE disclaimer_patterns SET label=?,pattern_text=?,kind=?,enabled=?,updated_at=datetime('now') WHERE pattern_id=?", (label, rule.text, rule.kind, rule.enabled, rule.id))
        conn.execute("INSERT OR REPLACE INTO sync_state(key,value,updated_at) VALUES ('auto_clean',?,datetime('now'))", ('1' if settings.autoClean else '0',))
    return read_api.settings(conn)


def save_preferences(conn, prefs):
    """Store only the fields supplied; the other preference is left as it was."""
    values = prefs.model_dump() if hasattr(prefs, 'model_dump') else prefs.dict()
    with conn:
        for name, key in read_api.PREFERENCE_KEYS.items():
            if values.get(name) is None:
                continue
            if any(len(path) > 1000 for path in values[name]):
                raise ValueError('Folder path is too long')
            conn.execute("INSERT OR REPLACE INTO sync_state(key,value,updated_at) VALUES (?,?,datetime('now'))",
                         (key, json.dumps(list(dict.fromkeys(values[name])), ensure_ascii=False)))
    return read_api.preferences(conn)


def install(app, database_path, tree_cache=None, store_name=None, source=None):
    """Register /api/dashboard routes without replacing existing /api routes.

    `source` replaces classic Outlook COM for Refresh; it needs
    folder_tree(progress) and fetch_folder(conn, path, since, progress),
    e.g. fetch_graph.GraphSource. Leave it None on the work computer.
    """
    database_path = str(Path(database_path).resolve())
    cache = Path(tree_cache) if tree_cache else Path(database_path).with_suffix('.folders.json')
    router = APIRouter(prefix='/api/dashboard')
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mail-dashboard')
    lock, jobs = Lock(), {}
    busy = False

    def connection():
        return db.connect(database_path)

    @app.middleware('http')
    async def local_requests(request: Request, call_next):
        if request.url.path.startswith('/api/dashboard'):
            from starlette.responses import JSONResponse
            host = request.url.hostname
            if host not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
                return JSONResponse({'detail': 'Local access only'}, status_code=403)
            client = request.client.host if request.client else ''
            try:
                local_client = ipaddress.ip_address(client).is_loopback
            except ValueError:
                local_client = client == 'testclient'
            if not local_client:
                return JSONResponse({'detail': 'Local client required'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Cross-origin access denied'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': 'Cross-site access denied'}, status_code=403)
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-outlook-digest') != '1':
                return JSONResponse({'detail': 'Missing request header'}, status_code=403)
        response = await call_next(request)
        # Without this, browsers reuse old frontend files by heuristic, so an
        # updated app.js can run against a stale adapter.js. Unchanged files
        # still revalidate cheaply as 304 Not Modified.
        if not request.url.path.startswith('/api/'):
            response.headers.setdefault('Cache-Control', 'no-cache')
        return response

    def start(action, use_com=False):
        nonlocal busy
        with lock:
            if busy:
                raise HTTPException(409, 'Another write job is running; wait for it to finish')
            busy = True
            ident = uuid.uuid4().hex
            jobs[ident] = dict(status='running', progress='Starting', result=None, error=None)
            # Bounded in-memory job history, not another database.
            for old in list(jobs)[:-50]:
                del jobs[old]

        def run():
            nonlocal busy
            conn, pythoncom = None, None
            def progress(text):
                with lock:
                    jobs[ident]['progress'] = text
            try:
                if use_com:
                    import pythoncom as com
                    com.CoInitialize()
                    pythoncom = com
                conn = connection()
                result = action(conn, progress)
                with lock:
                    jobs[ident].update(status='complete', progress='Done', result=result)
            except BaseException as exc:
                with lock:
                    jobs[ident].update(status='failed', error=str(exc))
            finally:
                if conn is not None:
                    conn.close()
                if pythoncom is not None:
                    pythoncom.CoUninitialize()
                with lock:
                    busy = False
        worker.submit(run)
        return {'job_id': ident}

    def read(fn, *args):
        conn = connection()
        try:
            return fn(conn, *args)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            conn.close()

    @router.get('/state')
    def state():
        result = read(read_api.dashboard_state)
        if store_name:
            result['account'] = store_name
        return result

    @router.post('/messages/query')
    def messages(filters: Filters):
        values = filters.model_dump(by_alias=True) if hasattr(filters, 'model_dump') else filters.dict(by_alias=True)
        return read(read_api.dashboard_messages, values)

    @router.get('/message')
    def message(key: str):
        result = read(read_api.dashboard_message, key)
        if result is None:
            raise HTTPException(404, 'Message not found')
        return result

    @router.get('/chains')
    def chains(folder: Optional[str] = None, limit: int = 500):
        return read(read_api.chains, folder, limit)

    @router.get('/chain')
    def chain(key: str):
        result = read(read_api.chain, key)
        if result is None:
            raise HTTPException(404, 'Chain not found')
        return result

    @router.get('/jobs/{ident}')
    def job(ident: str):
        with lock:
            if ident not in jobs:
                raise HTTPException(404, 'Job not found; server may have restarted')
            return dict(jobs[ident])

    @router.get('/people')
    def people():
        return read(read_api.people)

    @router.get('/preferences')
    def get_preferences():
        return read(read_api.preferences)

    @router.post('/preferences')
    def put_preferences(prefs: Preferences):
        # Tiny write, so not queued behind a long refresh job; the busy timeout
        # rides out a refresh's open transaction instead of failing at once.
        conn = connection()
        try:
            conn.execute('PRAGMA busy_timeout = 30000')
            return save_preferences(conn, prefs)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            conn.close()

    @router.post('/disclaimers')
    def disclaimers(settings: Settings):
        return start(lambda conn, progress: save_settings(conn, settings))

    @router.post('/clean')
    def clean(settings: Optional[Settings] = None):
        def action(conn, progress):
            if settings is not None:
                save_settings(conn, settings)
            progress('Normalizing and splitting pending messages')
            splitter.split_messages(conn, log=lambda *a: None)
            progress('Cleaning messages')
            cleaner.clean_messages(conn, log=lambda *a: None)
            return read_api.dashboard_state(conn)
        return start(action)

    @router.get('/folders')
    def folders():
        return json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else []

    @router.post('/folders/refresh')
    def refresh_folders():
        def action(conn, progress):
            if source is not None:
                tree = source.folder_tree(progress)
                temporary = cache.with_suffix('.tmp')
                temporary.write_text(json.dumps(tree), encoding='utf-8')
                temporary.replace(cache)
                return tree
            namespace = fetch._outlook_namespace()
            store = namespace.Folders[store_name] if store_name else namespace.GetDefaultFolder(fetch.OL_FOLDER_INBOX).Parent
            def walk(folder, path):
                progress(f'Reading {path}')
                children = []
                try:
                    for child in folder.Folders:
                        try:
                            children.append(walk(child, f'{path}/{child.Name}'))
                        except Exception:
                            continue
                except Exception:
                    pass
                return dict(path=path, name=folder.Name, children=children)
            tree = [walk(store, store.Name)]
            temporary = cache.with_suffix('.tmp')
            temporary.write_text(json.dumps(tree), encoding='utf-8')
            temporary.replace(cache)
            return tree
        return start(action, use_com=source is None)

    @router.post('/refresh')
    def refresh(body: Refresh):
        if body.mode not in ('last', 'cutoff', 'start'):
            raise HTTPException(422, 'Unknown refresh mode')
        if not 1 <= len(body.folders) <= 1000:
            raise HTTPException(422, 'Select between 1 and 1000 folders')
        def action(conn, progress):
            for path in dict.fromkeys(body.folders):
                progress(f'Fetching {path}')
                since = None
                if body.mode == 'cutoff':
                    cutoff = body.cutoff.model_dump() if hasattr(body.cutoff, 'model_dump') else body.cutoff.dict()
                    since = datetime.now(timezone.utc) - timedelta(**cutoff)
                elif body.mode == 'last':
                    row = conn.execute('SELECT value FROM sync_state WHERE key=?', ('last_fetch:' + path,)).fetchone()
                    if row:
                        since = datetime.fromisoformat(row[0].replace('Z', '+00:00')) - timedelta(minutes=5)
                if source is not None:
                    source.fetch_folder(conn, path, since, progress)
                else:
                    fetch.fetch_messages(conn, folder=path, since_days=0, since=since,
                                         use_restrict=False, strict=True, log=lambda *a: None)
            progress('Normalizing and splitting')
            splitter.split_messages(conn, log=lambda *a: None)
            if read_api.settings(conn)['autoClean']:
                progress('Cleaning')
                cleaner.clean_messages(conn, log=lambda *a: None)
            return read_api.dashboard_state(conn)
        return start(action, use_com=source is None)

    @app.on_event('shutdown')
    def shutdown():
        worker.shutdown(wait=True)

    app.include_router(router)
