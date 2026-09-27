"""Synthetic checks for the work computer. Never connects to Outlook."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import cleaner
import splitter
import textnorm
import read_api


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(':memory:', log=lambda *a: None)
        self.addCleanup(self.conn.close)

    def seed(self, key, conversation='thread', body='Hello', folder='Inbox', stamp='2026-09-11T18:00:00Z'):
        db.store_message(self.conn, dict(msg_key=key, conversation_id=conversation,
            body_raw=body, body_type='text', subject='Topic', sender_name='Sender',
            sender_addr='sender@example.com', sent_time=stamp, received_time=stamp,
            folder=folder), [('to', 'recipient@example.com', 'Recipient')])
        splitter.split_messages(self.conn, log=lambda *a: None)
        cleaner.clean_messages(self.conn, log=lambda *a: None)

    def test_chain_order_and_cross_folder_members(self):
        self.seed('later', body='Second', folder='Archive')
        self.seed('earlier', body='First', stamp='2026-09-10T18:00:00Z')
        chains = read_api.chains(self.conn, 'Inbox')
        self.assertEqual(chains[0]['count'], 2)
        body = read_api.chain(self.conn, 'c:thread')['body']
        self.assertLess(body.index('First'), body.index('Second'))

    def test_missing_conversation_ids_are_separate(self):
        self.seed('a', conversation=None)
        self.seed('b', conversation='')
        self.assertEqual({c['id'] for c in read_api.chains(self.conn)}, {'m:a', 'm:b'})

    def test_empty_cleaned_text_is_not_replaced(self):
        self.seed('a', body='Sent from my iPhone')
        self.assertEqual(read_api.dashboard_message(self.conn, 'a')['body'], '')

    def test_stale_split_never_exposes_quoted_body(self):
        self.seed('a', body='Hello\n----- Original Message -----\nOld secret')
        self.conn.execute('UPDATE boundary_patterns SET priority=priority+1')
        message = read_api.dashboard_message(self.conn, 'a')
        self.assertTrue(message['pending'])
        self.assertNotIn('Old secret', message['body'])

    def test_filters_and_metadata_only(self):
        self.seed('a')
        result = read_api.dashboard_messages(self.conn, {'to': 'recipient', 'evenings': True})
        self.assertEqual(len(result), 1)
        self.assertNotIn('body', result[0])
        self.assertEqual(read_api.dashboard_messages(self.conn, {'subject': "' OR 1=1 --"}), [])
        with self.assertRaises(ValueError):
            read_api.dashboard_messages(self.conn, {'limit': -1})

    def test_multi_folder_filter(self):
        self.seed('a', conversation='x', folder='Inbox')
        self.seed('b', conversation='y', folder='Inbox/UKVI')
        self.seed('c', conversation='z', folder='Archive')
        keys = lambda f: {m['id'] for m in read_api.dashboard_messages(self.conn, f)}
        self.assertEqual(keys({'folders': ['Inbox', 'Inbox/UKVI']}), {'a', 'b'})
        self.assertEqual(keys({'folders': []}), {'a', 'b', 'c'})

    def test_multiple_people_match_any(self):
        for key, sender, to, cc in [('a', 'ann@x.com', 'bob@x.com', 'cat@x.com'),
                                    ('b', 'bob@x.com', 'ann@x.com', 'dan@x.com'),
                                    ('c', 'eve@x.com', 'eve@x.com', 'eve@x.com')]:
            db.store_message(self.conn, dict(msg_key=key, conversation_id=key, body_raw='Hi', body_type='text',
                subject='Topic', sender_name=None, sender_addr=sender, sent_time='2026-09-11T10:00:00Z',
                received_time='2026-09-11T10:00:00Z', folder='Inbox'), [('to', to, None), ('cc', cc, None)])
        keys = lambda f: {m['id'] for m in read_api.dashboard_messages(self.conn, f)}
        self.assertEqual(keys({'from': ['ann@x.com', 'bob@x.com']}), {'a', 'b'})
        self.assertEqual(keys({'to': ['bob@x.com', 'eve']}), {'a', 'c'})
        self.assertEqual(keys({'to': ['cat@x.com'], 'includeCC': True}), {'a'})
        self.assertEqual(keys({'from': ['ann@x.com', 'bob@x.com'], 'cc': ['dan@x.com']}), {'b'})
        self.assertEqual(keys({'from': 'eve'}), {'c'})
        self.assertEqual(keys({'from': [], 'to': ['  ']}), {'a', 'b', 'c'})
        people = read_api.people(self.conn)
        self.assertEqual([p['value'] for p in people['from']], ['ann@x.com', 'bob@x.com', 'eve@x.com'])
        self.assertEqual([p['value'] for p in people['cc']], ['cat@x.com', 'dan@x.com', 'eve@x.com'])

    def test_existing_work_server_read_functions_remain_available(self):
        self.seed('a')
        self.assertEqual(read_api.state(self.conn)['messages'], 1)
        self.assertEqual(read_api.messages(self.conn, q='Topic')[0]['msg_key'], 'a')
        self.assertEqual(read_api.message(self.conn, 'a')['unique_body_text'], 'Hello')
        self.assertTrue(read_api.boundary_patterns(self.conn))

    def test_normalization_invalidates_all_downstream_results(self):
        self.seed('a')
        textnorm.normalize_messages(self.conn, rebuild=True, log=lambda *a: None)
        self.assertEqual(self.conn.execute('SELECT unique_body_text, cleaned_unique_body_text, cleaner_version FROM messages').fetchone(), (None, None, None))

    def test_cache_is_reused_on_split_rebuild(self):
        from unittest.mock import patch
        self.seed('a')
        with patch.object(textnorm, 'to_text', side_effect=AssertionError('HTML reparsed')):
            splitter.split_messages(self.conn, rebuild=True, log=lambda *a: None)

    def test_schema_migration_preserves_original_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'legacy.db')
            schema = Path(db.SCHEMA).read_text(encoding='utf-8')
            schema = schema.replace('cleaned_unique_body_text', 'cleaned_content').replace('unique_body_text', 'content')
            conn = sqlite3.connect(path)
            conn.executescript(schema)
            conn.execute("INSERT INTO messages(msg_key,body_raw,content,cleaned_content) VALUES ('a','original','individual','cleaned')")
            conn.commit(); conn.close()
            conn = db.init(path, log=lambda *a: None)
            try:
                self.assertEqual(conn.execute('SELECT body_raw,unique_body_text,cleaned_unique_body_text FROM messages').fetchone(), ('original', 'individual', 'cleaned'))
            finally:
                conn.close()


class WebTests(unittest.TestCase):
    def test_read_routes_and_cross_origin_protection(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import dashboard_api
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'mail.db')
            conn = db.init(path, log=lambda *a: None); conn.close()
            app = FastAPI()
            dashboard_api.install(app, path)
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/dashboard/state').status_code, 200)
                self.assertEqual(client.post('/api/dashboard/messages/query', json={}).status_code, 403)
                headers = {'X-Outlook-Digest': '1'}
                self.assertEqual(client.post('/api/dashboard/messages/query', json={}, headers=headers).json(), [])
                self.assertEqual(client.post('/api/dashboard/messages/query', json={}, headers={**headers, 'Origin': 'https://untrusted.example'}).status_code, 403)

    def test_preferences_persist_in_database(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import dashboard_api
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'mail.db')
            conn = db.init(path, log=lambda *a: None); conn.close()
            app = FastAPI()
            dashboard_api.install(app, path)
            headers = {'X-Outlook-Digest': '1'}
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/dashboard/preferences').json(),
                                 {'folderOrder': None, 'refreshChecked': None})
                self.assertEqual(client.post('/api/dashboard/preferences', json={'refreshChecked': ['Inbox']}).status_code, 403)
                client.post('/api/dashboard/preferences', headers=headers,
                            json={'folderOrder': ['Inbox', 'Inbox/UKVI'], 'refreshChecked': ['Inbox/UKVI']})
                client.post('/api/dashboard/preferences', headers=headers, json={'refreshChecked': []})
            conn = db.connect(path)
            try:
                self.assertEqual(read_api.preferences(conn),
                                 {'folderOrder': ['Inbox', 'Inbox/UKVI'], 'refreshChecked': []})
            finally:
                conn.close()

    def test_refresh_uses_supplied_source_for_ticked_folders_only(self):
        import time
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import dashboard_api

        class FakeSource:
            def __init__(self):
                self.calls = []
            def folder_tree(self, progress):
                return [dict(path='Inbox', name='Inbox', children=[])]
            def fetch_folder(self, conn, path, since, progress):
                self.calls.append((path, since))
                return 0

        source = FakeSource()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'mail.db')
            conn = db.init(path, log=lambda *a: None); conn.close()
            app = FastAPI()
            dashboard_api.install(app, path, source=source)
            headers = {'X-Outlook-Digest': '1'}
            with TestClient(app) as client:
                job = client.post('/api/dashboard/refresh', headers=headers, json={
                    'mode': 'cutoff', 'folders': ['Inbox/UKVI'], 'cutoff': {'days': 50}}).json()['job_id']
                for _ in range(50):
                    state = client.get(f'/api/dashboard/jobs/{job}').json()
                    if state['status'] != 'running':
                        break
                    time.sleep(0.05)
                self.assertEqual(state['status'], 'complete', state.get('error'))
        self.assertEqual([p for p, _ in source.calls], ['Inbox/UKVI'])
        self.assertIsNotNone(source.calls[0][1])

    def test_deleted_disclaimers_do_not_return_on_restart(self):
        from dashboard_api import save_settings, Settings
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'mail.db')
            conn = db.init(path, log=lambda *a: None)
            save_settings(conn, Settings(disclaimers=[], autoClean=True))
            conn.close()
            conn = db.init(path, log=lambda *a: None)
            try:
                self.assertEqual(read_api.settings(conn), {'disclaimers': [], 'autoClean': True})
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
