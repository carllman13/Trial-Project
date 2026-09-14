"""Database reads and chain formatting. No web framework or Outlook dependency."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import sqlite3

import cleaner
import splitter
import textnorm

CHAIN_KEY = "CASE WHEN NULLIF(conversation_id, '') IS NULL THEN 'm:' || msg_key ELSE 'c:' || conversation_id END"
STAMP = "COALESCE(sent_time, received_time, '')"


def rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def configure(conn):
    # Explicit London conversion: fixed UTC offsets are wrong around DST.
    london = ZoneInfo('Europe/London')
    def local(value, part):
        if not value:
            return ''
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(london)
        return stamp.date().isoformat() if part == 'date' else stamp.hour
    conn.create_function('london_date', 1, lambda v: local(v, 'date'))
    conn.create_function('london_hour', 1, lambda v: local(v, 'hour'))


def versions(conn):
    split_rules, split_errors = splitter.load_patterns(conn)
    clean_rules, clean_errors = cleaner.load_patterns(conn)
    return (None if split_errors else splitter.fingerprint(split_rules),
            None if clean_errors else cleaner.fingerprint(clean_rules))


def display_body(row, current):
    split_version, clean_version = current
    if (split_version is None or row['textnorm_version'] != textnorm.CODE_VERSION
            or row['boundary_patterns_version'] != split_version
            or row['unique_body_text'] is None):
        return '[Individual content pending processing]', True
    # Empty strings are legitimate cleaning results. Never use truthiness here.
    if (clean_version is not None and row['cleaner_version'] == clean_version
            and row['cleaned_unique_body_text'] is not None):
        return row['cleaned_unique_body_text'], False
    return row['unique_body_text'], False


def _person(name, addr):
    return f'{name} <{addr}>' if name and addr else (addr or name or '')


def summary(conn, row):
    recipients = rows(conn, 'SELECT role, name, addr FROM participants WHERE msg_key=? ORDER BY role, addr', (row['msg_key'],))
    def addresses(role):
        return '; '.join(_person(p['name'], p['addr']) for p in recipients if p['role'] == role)
    names = row.get('attachment_names')
    return dict(id=row['msg_key'], chainId=row['chain_key'], folder=row['folder'] or '',
                received=row['received_time'] or row['sent_time'],
                sent=row['sent_time'], subject=row['subject'] or '',
                **{'from': _person(row['sender_name'], row['sender_addr'])},
                to=addresses('to'), cc=addresses('cc'),
                attachments=len(names.splitlines()) if names else (1 if row['has_attachments'] else 0),
                lastSeen=row.get('first_ingested') or '')


def dashboard_messages(conn, filters=None):
    """Return bounded metadata only. Bodies are requested separately."""
    configure(conn)
    f = filters or {}
    limit = int(f.get('limit') or 500)
    if not 1 <= limit <= 5000:
        raise ValueError('Limit must be between 1 and 5000')
    clauses, params = [], []
    if f.get('folder'):
        clauses.append('m.folder=?'); params.append(f['folder'])
    for field, op in (('fromDate', '>='), ('toDate', '<=')):
        if f.get(field):
            datetime.strptime(f[field], '%Y-%m-%d')
            clauses.append(f'london_date(m.received_time) {op} ?'); params.append(f[field])
    if f.get('fromDate') and f.get('toDate') and f['fromDate'] > f['toDate']:
        raise ValueError('From date must not be after To date')
    if f.get('days'):
        days = int(f['days'])
        if days < 0:
            raise ValueError('Last N days must not be negative')
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        clauses.append('m.received_time >= ?'); params.append(cutoff.strftime('%Y-%m-%dT%H:%M:%SZ'))
    split_version, clean_version = versions(conn)
    for field, sql in [('subject', 'm.subject'), ('from', "COALESCE(m.sender_name,'') || ' ' || COALESCE(m.sender_addr,'')")]:
        if f.get(field):
            clauses.append(f'instr(lower({sql}), lower(?)) > 0'); params.append(f[field])
    if f.get('body'):
        clauses.append("m.textnorm_version=? AND m.boundary_patterns_version=? AND instr(lower(CASE WHEN m.cleaner_version=? AND m.cleaned_unique_body_text IS NOT NULL THEN m.cleaned_unique_body_text ELSE COALESCE(m.unique_body_text,'') END),lower(?))>0")
        params.extend([textnorm.CODE_VERSION, split_version, clean_version, f['body']])
    for field in ('to', 'cc'):
        if f.get(field):
            roles = "('to','cc')" if field == 'to' and f.get('includeCC') else f"('{field}')"
            clauses.append(f"EXISTS (SELECT 1 FROM participants p WHERE p.msg_key=m.msg_key AND p.role IN {roles} AND instr(lower(COALESCE(p.name,'') || ' ' || p.addr),lower(?))>0)")
            params.append(f[field])
    if f.get('evenings'):
        clauses.append('london_hour(m.received_time) >= 18 AND london_hour(m.received_time) < 22')
    where = ' AND '.join(clauses) or '1=1'
    selected = rows(conn, f"SELECT msg_key, conversation_id, {CHAIN_KEY} AS chain_key, subject, sender_name, sender_addr, sent_time, received_time, folder, has_attachments, attachment_names, first_ingested FROM messages m WHERE {where} ORDER BY received_time DESC, msg_key LIMIT ?", [*params, limit])
    return [summary(conn, row) for row in selected]


def dashboard_message(conn, key):
    result = rows(conn, f'SELECT *, {CHAIN_KEY} AS chain_key FROM messages WHERE msg_key=?', (key,))
    if not result:
        return None
    row = result[0]
    body, pending = display_body(row, versions(conn))
    return dict(summary(conn, row), body=body, rawBody=row['body_raw'] or '', pending=pending)


def chains(conn, folder=None, limit=500):
    """Folder selects conversations; counts and details include all stored members."""
    if not 1 <= limit <= 5000:
        raise ValueError('Limit must be between 1 and 5000')
    selected = rows(conn, f'''WITH members AS (
        SELECT msg_key, conversation_id, subject, sender_name, sender_addr,
               sent_time, received_time, folder, has_attachments, attachment_names,
               first_ingested, {CHAIN_KEY} AS chain_key FROM messages
    ), ranked AS (
        SELECT *, COUNT(*) OVER (PARTITION BY chain_key) AS n,
               ROW_NUMBER() OVER (PARTITION BY chain_key ORDER BY {STAMP} DESC, msg_key DESC) AS rank
        FROM members
    ) SELECT * FROM ranked r WHERE rank=1 AND
        (? IS NULL OR EXISTS (SELECT 1 FROM members s WHERE s.chain_key=r.chain_key AND s.folder=?))
        ORDER BY {STAMP} DESC, msg_key DESC LIMIT ?''', (folder, folder, limit))
    # Do not send the body fields selected by the window query to the browser.
    return [dict(summary(conn, row), id=row['chain_key'], count=row['n'], folder=folder or row['folder'] or '') for row in selected]


def chain(conn, key):
    if key.startswith('c:'):
        where, value = 'conversation_id=?', key[2:]
    elif key.startswith('m:'):
        where, value = "msg_key=? AND NULLIF(conversation_id,'') IS NULL", key[2:]
    else:
        raise ValueError('Invalid chain ID')
    members = rows(conn, f'SELECT *, {CHAIN_KEY} AS chain_key FROM messages WHERE {where} ORDER BY {STAMP}, msg_key', (value,))
    if not members:
        return None
    current, blocks = versions(conn), []
    for row in members:
        body, pending = display_body(row, current)
        info = summary(conn, row)
        blocks.append(f"Date: {row['sent_time'] or row['received_time'] or ''}\nFrom: {info['from']}\nTo: {info['to']}\nCc: {info['cc']}\nSubject: {info['subject']}\n\n{body}")
    latest = summary(conn, members[-1])
    return dict(latest, id=key, count=len(members), body=('\n\n' + '=' * 72 + '\n\n').join(blocks))


def settings(conn):
    rules = rows(conn, 'SELECT pattern_id AS id, label, pattern_text AS text, kind, enabled FROM disclaimer_patterns ORDER BY pattern_id')
    auto = conn.execute("SELECT value FROM sync_state WHERE key='auto_clean'").fetchone()
    return dict(disclaimers=rules, autoClean=bool(auto and auto[0] == '1'))


def dashboard_state(conn):
    total = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    stamp = conn.execute("SELECT MAX(value) FROM sync_state WHERE key LIKE 'last_fetch:%'").fetchone()[0]
    folders = [r[0] for r in conn.execute("SELECT DISTINCT folder FROM messages WHERE folder IS NOT NULL ORDER BY folder")]
    return dict(account='Local mailbox', lastSynced=stamp or '', totalRows=total, folders=folders,
                messages=[], chains=[], prompts=[], **settings(conn))


# Existing work-server API. Keep these names and response shapes stable because
# the work app's original /api routes already call them.
def folders_in_db(conn):
    return rows(conn,
        "SELECT COALESCE(folder, '(unknown)') AS folder, COUNT(*) AS n "
        "FROM messages GROUP BY folder ORDER BY n DESC")


def state(conn):
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    needs_pattern = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE boundary_patterns_version IS NOT NULL "
        "AND boundary_pattern_id IS NULL").fetchone()[0]
    unsplit_originals = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE unique_body_text IS NULL").fetchone()[0]
    split_rules, split_broken = splitter.load_patterns(conn)
    split_version = splitter.fingerprint(split_rules)
    stale_split = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE body_text IS NOT NULL AND "
        "(boundary_patterns_version IS NULL OR boundary_patterns_version != ?)",
        (split_version,)).fetchone()[0]
    clean_rules, clean_broken = cleaner.load_patterns(conn)
    clean_version = cleaner.fingerprint(clean_rules)
    stale_clean = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE unique_body_text IS NOT NULL AND "
        "(cleaner_version IS NULL OR cleaner_version != ?)",
        (clean_version,)).fetchone()[0]
    last_sync = conn.execute(
        "SELECT MAX(value) FROM sync_state WHERE key LIKE 'last_fetch:%'").fetchone()[0]
    return dict(messages=total, unsplit_needs_pattern=needs_pattern,
                unsplit_originals=unsplit_originals, stale_split=stale_split,
                stale_clean=stale_clean, last_sync=last_sync,
                boundary_patterns_broken=len(split_broken),
                disclaimer_patterns_broken=len(clean_broken),
                folders=folders_in_db(conn))


def messages(conn, folder=None, q=None, limit=200):
    """Existing compact list response; never selects a message body."""
    clauses, params = [], []
    if folder:
        clauses.append("m.folder = ?")
        params.append(folder)
    if q:
        clauses.append("(m.subject LIKE ? OR m.sender_name LIKE ? OR m.sender_addr LIKE ?)")
        params.extend([f"%{q}%"] * 3)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return rows(conn, f"""
        SELECT m.msg_key, m.sent_time, m.received_time, m.sender_name,
               m.sender_addr, m.subject, m.folder, m.has_attachments,
               m.attachment_names, b.label AS boundary_label,
               length(m.body_raw) AS raw_len, length(m.body_text) AS text_len,
               length(m.unique_body_text) AS uniq_len,
               length(m.cleaned_unique_body_text) AS clean_len
        FROM messages m
        LEFT JOIN boundary_patterns b ON b.pattern_id = m.boundary_pattern_id
        {where} ORDER BY m.sent_time DESC LIMIT ?""", [*params, limit])


def message(conn, msg_key):
    result = rows(conn, """
        SELECT m.*, b.label AS boundary_label FROM messages m
        LEFT JOIN boundary_patterns b ON b.pattern_id = m.boundary_pattern_id
        WHERE m.msg_key = ?""", (msg_key,))
    if not result:
        return None
    result[0]['participants'] = rows(conn,
        "SELECT role, addr, name FROM participants WHERE msg_key = ? "
        "ORDER BY CASE role WHEN 'from' THEN 0 WHEN 'to' THEN 1 "
        "WHEN 'cc' THEN 2 ELSE 3 END, addr", (msg_key,))
    return result[0]


def boundary_patterns(conn):
    return rows(conn,
        "SELECT pattern_id, label, priority, enabled, line_regex, "
        "require_regex, confirm_regex, confirm_within, example_block, notes "
        "FROM boundary_patterns ORDER BY priority, pattern_id")
