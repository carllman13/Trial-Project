"""Repeat-import regression tests: synthetic data only, no Outlook connection."""
import datetime as dt
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cleaner
import cli
import db
import fetch
import splitter


def message(key='<test@example.com>', folder='Inbox'):
    return dict(msg_key=key, folder=folder, sender_addr='a@example.com',
                body_type='text', body_raw='Hello\nSent from my iPhone')


class Items(list):
    def Sort(self, *args):
        pass


def folder(name, items=(), children=()):
    return SimpleNamespace(Name=name, FolderPath=name,
                           Items=Items(items), Folders=children)


def item(key, received=None):
    return SimpleNamespace(
        Class=43, ReceivedTime=received,
        PropertyAccessor=SimpleNamespace(GetProperty=lambda tag: key))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(':memory:', log=lambda *a: None)
        self.addCleanup(self.conn.close)

    def seed(self):
        db.store_message(self.conn, message(), [('from', 'a@example.com', 'A')])
        splitter.split_messages(self.conn, log=lambda *a: None)
        cleaner.clean_messages(self.conn, log=lambda *a: None)
        self.conn.execute("UPDATE messages SET first_ingested = '2000-01-01'")
        self.conn.commit()

    def snapshot(self):
        return tuple(self.conn.execute('SELECT * FROM ' + table).fetchall()
                     for table in ('messages', 'participants', 'disclaimer_hits'))

    def scan(self, root, **kwargs):
        namespace = SimpleNamespace(GetDefaultFolder=lambda n: root)
        with patch.object(fetch, '_outlook_namespace', return_value=namespace):
            return fetch.fetch_messages(self.conn, log=lambda *a: None, **kwargs)

    def test_same_folder_is_no_write_even_if_input_differs(self):
        self.seed()
        before = self.snapshot()
        changes = self.conn.total_changes
        status = db.store_message(self.conn, {**message(), 'body_raw': 'edited',
                                             'sender_addr': None}, [])
        self.assertEqual(status, 'unchanged')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.total_changes, changes)

    def test_move_only_updates_folder(self):
        self.seed()
        before = self.snapshot()
        self.assertEqual(db.store_message(self.conn, message(folder='Archive')), 'moved')
        self.assertEqual(self.conn.execute('SELECT folder FROM messages').fetchone()[0], 'Archive')
        self.conn.execute("UPDATE messages SET folder = 'Inbox'")
        self.assertEqual(self.snapshot(), before)

    def test_new_message_is_unprocessed_and_caller_can_rollback(self):
        self.assertEqual(db.store_message(self.conn, message()), 'inserted')
        self.assertEqual(self.conn.execute('SELECT content, cleaned_content FROM messages').fetchone(), (None, None))
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_bad_recipient_rolls_back_only_its_message(self):
        db.store_message(self.conn, message('<good>'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.store_message(self.conn, message('<bad>'),
                             [('to', 'a@example.com', 'A'), ('invalid', 'b@example.com', 'B')])
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT msg_key FROM messages').fetchall(), [('<good>',)])
        self.assertEqual(self.conn.execute('SELECT * FROM participants').fetchall(), [])

    def test_existing_fetch_skips_body_and_address_extraction(self):
        self.seed()
        with patch.object(fetch, 'message_from_item', side_effect=AssertionError('must not extract')) as extract:
            self.assertEqual(self.scan(folder('Archive', [item('<test@example.com>')]), since_days=0), 0)
            extract.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT folder FROM messages').fetchone()[0], 'Archive')

    def test_limit_includes_existing_and_stops_before_children(self):
        for i in range(7):
            db.store_message(self.conn, message(str(i)))
        child = folder('Child', [item('6')])
        root = folder('Archive', [item(str(i)) for i in range(6)], [child])
        self.scan(root, since_days=0, recurse=True, limit=5)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages WHERE folder='Archive'").fetchone()[0], 5)
        self.assertIsNone(self.conn.execute("SELECT value FROM sync_state WHERE key='last_fetch:Child'").fetchone())

    def test_python_cutoff_applies_before_existing_folder_update(self):
        self.seed()
        old = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        self.scan(folder('Archive', [item('<test@example.com>', old)]), since_days=1, use_restrict=False)
        self.assertEqual(self.conn.execute('SELECT folder FROM messages').fetchone()[0], 'Inbox')

    def test_mixed_scan_extracts_only_new_and_reports_counts(self):
        self.seed()
        root = folder('Inbox', [item('<test@example.com>'), item('<new>')])
        namespace = SimpleNamespace(GetDefaultFolder=lambda n: root)
        logs = []
        with patch.object(fetch, '_outlook_namespace', return_value=namespace), patch.object(
                fetch, 'message_from_item', return_value=(message('<new>'), [])) as extract:
            self.assertEqual(fetch.fetch_messages(self.conn, since_days=0, log=logs.append), 1)
            extract.assert_called_once()
        self.assertTrue(any('stored 1 new; moved 0; unchanged 1' in s for s in logs))

    def test_explicit_cli_limit_five_is_passed_through(self):
        with patch('sys.argv', ['cli.py', ':memory:', 'fetch', '--limit', '5']), patch.object(
                fetch, 'fetch_messages') as scan:
            cli.main()
            self.assertEqual(scan.call_args.kwargs['limit'], 5)


if __name__ == '__main__':
    unittest.main()
