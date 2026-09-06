"""Offline processing checks for the corporate machine; no Outlook access."""
import unittest
from unittest.mock import patch

import cleaner
import db
import splitter
import textnorm


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(":memory:", log=lambda *a: None)
        self.addCleanup(self.conn.close)
        self.raw = "Hello\nSent from my iPhone\n----- Original Message -----\nOld"
        db.store_message(self.conn, dict(msg_key="<test>", body_raw=self.raw,
                                        body_type="text"))
        self.split()
        self.clean()

    def split(self, **kwargs):
        return splitter.split_messages(self.conn, log=lambda *a: None, **kwargs)

    def clean(self, **kwargs):
        return cleaner.clean_messages(self.conn, log=lambda *a: None, **kwargs)

    def snapshot(self):
        return (self.conn.execute("SELECT * FROM messages").fetchall(),
                self.conn.execute("SELECT * FROM disclaimer_hits").fetchall())

    def test_resplit_clears_cleaned_text_and_hits(self):
        self.split(rebuild=True)
        self.assertEqual(self.conn.execute(
            "SELECT cleaned_content, cleaner_version FROM messages").fetchone(),
            (None, None))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM disclaimer_hits").fetchone()[0], 0)
        self.clean()
        self.assertEqual(self.conn.execute(
            "SELECT cleaned_content FROM messages").fetchone()[0], "Hello")

    def test_disabled_cleaners_restore_content(self):
        self.conn.execute("UPDATE disclaimer_patterns SET enabled=0")
        self.assertEqual(self.clean(), 1)
        content, cleaned = self.conn.execute(
            "SELECT content, cleaned_content FROM messages").fetchone()
        self.assertEqual(cleaned, content)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM disclaimer_hits").fetchone()[0], 0)
        self.assertEqual(self.clean(), 0)

    def test_disabled_boundaries_restore_full_body(self):
        self.conn.execute("UPDATE boundary_patterns SET enabled=0")
        self.assertEqual(self.split(), 1)
        self.assertEqual(self.conn.execute(
            "SELECT content, boundary_pattern_id FROM messages").fetchone(),
            (self.raw, None))
        self.assertEqual(self.split(), 0)

    def test_window_and_priority_changes_trigger_processing(self):
        for column in ("confirm_within", "priority"):
            with self.subTest(column=column):
                self.conn.execute("UPDATE boundary_patterns SET " + column +
                                  " = " + column + " + 1")
                self.assertEqual(self.split(), 1)
                self.assertEqual(self.split(), 0)

    def test_matching_flags_are_part_of_cleaner_version(self):
        literal = [(1, "x", cleaner.build_matcher("Footer", "literal"))]
        regex = [(1, "x", cleaner.build_matcher("Footer", "regex"))]
        self.assertNotEqual(cleaner.fingerprint(literal), cleaner.fingerprint(regex))

    def test_text_conversion_version_triggers_processing(self):
        with patch.object(textnorm, "CODE_VERSION", textnorm.CODE_VERSION + 1):
            self.assertEqual(self.split(), 1)
            self.assertEqual(self.clean(), 1)
            self.assertEqual(self.split(), 0)
            self.assertEqual(self.clean(), 0)

    def test_invalid_rules_do_not_write_results(self):
        for table, column, process in (
            ("boundary_patterns", "line_regex", self.split),
            ("disclaimer_patterns", "pattern_text", self.clean),
        ):
            with self.subTest(table=table):
                if table == "disclaimer_patterns":
                    self.conn.execute("UPDATE disclaimer_patterns SET kind='regex'")
                self.conn.execute("UPDATE " + table + " SET " + column + "='['")
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    process()
                self.assertEqual(self.snapshot(), before)

    def test_dry_run_keeps_results(self):
        before = self.snapshot()
        self.split(rebuild=True, dry_run=True)
        self.clean(rebuild=True, dry_run=True)
        self.assertEqual(self.snapshot(), before)

    def test_html_cells_and_rows_stay_separate(self):
        html = ("<table><tr><th>Field</th><th>Value</th></tr>"
                "<tr><td>Principal</td><td><b>100</b></td></tr></table>")
        lines = [line for line in textnorm.html_to_text(html).splitlines() if line]
        self.assertEqual(lines, ["Field\tValue", "Principal\t100"])

    def test_raw_body_is_never_changed(self):
        self.split(rebuild=True)
        self.clean(rebuild=True)
        self.assertEqual(self.conn.execute(
            "SELECT body_raw FROM messages").fetchone()[0], self.raw)


if __name__ == "__main__":
    unittest.main()
