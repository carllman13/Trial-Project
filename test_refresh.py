"""Repeat-import regression tests: synthetic data only, no Outlook connection."""
import sqlite3
import unittest

import cleaner
import cli
import db
import splitter


def message(key='<test@example.com>', folder='Inbox'):
    return dict(msg_key=key, folder=folder, sender_addr='a@example.com',
                body_type='text', body_raw='Hello\nSent from my iPhone')


class Items(list):
    def Sort(self, *args):
        pass




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

if __name__ == '__main__':
    unittest.main()
