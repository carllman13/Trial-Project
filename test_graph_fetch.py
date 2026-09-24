"""Microsoft Graph fetch helpers -- no network, synthetic Graph JSON only."""
import unittest
from unittest.mock import patch

import db
import graph_auth
import graph_fetch


def graph_message(**overrides):
    base = {
        "id": "AAMkAG",
        "internetMessageId": "<g1@outlook.com>",
        "conversationId": "conv1",
        "subject": "Hello",
        "from": {"emailAddress": {"name": "Carl Wei",
                                  "address": "carlwei2017@outlook.com"}},
        "toRecipients": [
            {"emailAddress": {"name": "Jane", "address": "jane@acme.com"}},
        ],
        "ccRecipients": [],
        "bccRecipients": [],
        "sentDateTime": "2026-09-01T09:14:00Z",
        "receivedDateTime": "2026-09-01T09:14:22Z",
        "hasAttachments": False,
        "body": {"contentType": "html", "content": "<p>Hi</p>"},
    }
    base.update(overrides)
    return base


class GraphMappingTests(unittest.TestCase):
    def test_message_maps_to_store_contract(self):
        msg, parts = graph_fetch.message_from_graph(
            graph_message(), folder_path="Inbox")
        self.assertEqual(msg["msg_key"], "<g1@outlook.com>")
        self.assertEqual(msg["conversation_id"], "conv1")
        self.assertEqual(msg["sender_addr"], "carlwei2017@outlook.com")
        self.assertEqual(msg["sender_name"], "Carl Wei")
        self.assertEqual(msg["body_raw"], "<p>Hi</p>")
        self.assertEqual(msg["body_type"], "html")
        self.assertEqual(msg["folder"], "Inbox")
        self.assertEqual(msg["has_attachments"], 0)
        self.assertEqual(msg["recipients_dropped"], 0)
        self.assertEqual(parts[0], ("from", "carlwei2017@outlook.com", "Carl Wei"))
        self.assertEqual(parts[1], ("to", "jane@acme.com", "Jane"))

    def test_missing_internet_message_id_skipped(self):
        self.assertIsNone(graph_fetch.message_from_graph(
            graph_message(internetMessageId=None)))

    def test_text_body_and_dropped_recipients(self):
        item = graph_message(
            body={"contentType": "text", "content": "plain"},
            toRecipients=[{"emailAddress": {"name": "No Addr"}}],
            ccRecipients=[{"emailAddress": {"address": "cc@acme.com"}}],
        )
        msg, parts = graph_fetch.message_from_graph(item, "Inbox")
        self.assertEqual(msg["body_type"], "text")
        self.assertEqual(msg["recipients_dropped"], 1)
        roles = [p[0] for p in parts]
        self.assertIn("cc", roles)
        self.assertNotIn("to", roles)

    def test_to_utc_iso_normalises(self):
        self.assertEqual(graph_fetch.to_utc_iso("2026-09-01T09:14:00Z"),
                         "2026-09-01T09:14:00Z")
        self.assertEqual(graph_fetch.to_utc_iso("2026-09-01T09:14:00+00:00"),
                         "2026-09-01T09:14:00Z")
        self.assertIsNone(graph_fetch.to_utc_iso(None))


class GraphFetchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.init(":memory:", log=lambda *a: None)
        self.addCleanup(self.conn.close)

    def test_fetch_stores_through_db_and_leaves_derived_null(self):
        pages = {
            "inbox": {"id": "folder-inbox", "displayName": "Inbox"},
            "messages": {"value": [graph_message()], "@odata.nextLink": None},
        }

        def fake_get(url, token, params=None):
            if url.endswith("/me"):
                return {"displayName": "Carl",
                        "mail": "carlwei2017@outlook.com"}
            if url.rstrip("/").endswith("/mailFolders/inbox"):
                return pages["inbox"]
            if "/messages" in url:
                return pages["messages"]
            raise AssertionError(f"unexpected url {url}")

        with patch.object(graph_auth, "acquire_token", return_value="tok"), \
             patch.object(graph_auth, "graph_get", side_effect=fake_get):
            n = graph_fetch.fetch_messages(
                self.conn, since_days=30, log=lambda *a: None, token="tok")
        self.assertEqual(n, 1)
        row = self.conn.execute(
            "SELECT msg_key, body_raw, body_text, unique_body_text, "
            "cleaned_unique_body_text, folder FROM messages"
        ).fetchone()
        self.assertEqual(row[0], "<g1@outlook.com>")
        self.assertEqual(row[1], "<p>Hi</p>")
        self.assertEqual(row[2:5], (None, None, None))
        self.assertEqual(row[5], "Inbox")
        parts = self.conn.execute(
            "SELECT role, addr FROM participants ORDER BY role"
        ).fetchall()
        self.assertEqual(parts, [("from", "carlwei2017@outlook.com"),
                                 ("to", "jane@acme.com")])

    def test_existing_message_skips_body_reread(self):
        db.store_message(
            self.conn,
            dict(msg_key="<g1@outlook.com>", folder="Inbox",
                 body_raw="original", body_type="text",
                 sender_addr="carlwei2017@outlook.com"),
            [("from", "carlwei2017@outlook.com", "Carl")],
        )
        self.conn.commit()

        def fake_get(url, token, params=None):
            if url.endswith("/me"):
                return {"mail": "carlwei2017@outlook.com"}
            if url.rstrip("/").endswith("/mailFolders/inbox"):
                return {"id": "folder-inbox", "displayName": "Inbox"}
            if "/messages" in url:
                # Body deliberately different -- must not overwrite archive.
                return {"value": [graph_message(
                    body={"contentType": "html", "content": "<p>CHANGED</p>"})]}
            raise AssertionError(url)

        with patch.object(graph_auth, "graph_get", side_effect=fake_get), \
             patch.object(graph_fetch, "message_from_graph",
                          side_effect=AssertionError("must not extract")):
            n = graph_fetch.fetch_messages(
                self.conn, since_days=0, log=lambda *a: None, token="tok")
        self.assertEqual(n, 0)
        self.assertEqual(
            self.conn.execute("SELECT body_raw FROM messages").fetchone()[0],
            "original")


class AuthScaffoldTests(unittest.TestCase):
    def test_missing_auth_message_is_actionable(self):
        with patch.dict("os.environ", {}, clear=True):
            # Clear only Graph-related keys while keeping PATH etc.
            pass
        env = {k: v for k, v in __import__("os").environ.items()
               if not k.startswith("MS_GRAPH") and k != "OUTLOOK_ACCOUNT"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(graph_auth.AuthError) as ctx:
                graph_auth.acquire_token(interactive=False)
            text = str(ctx.exception)
            self.assertIn("MS_GRAPH_ACCESS_TOKEN", text)
            self.assertIn("MS_GRAPH_CLIENT_ID", text)


if __name__ == "__main__":
    unittest.main()
