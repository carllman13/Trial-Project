"""Build a demo database with realistic mail so the cleaner can be tested
without touching Outlook.

The disclaimers below are deliberately line-wrapped differently in each
message -- that is exactly what Outlook desktop vs mobile vs web do to the
same footer, and it is what breaks naive exact-string matching.
"""
import sqlite3
import sys

import outlook_reader

DISCLAIMER = (
    "This e-mail and any attachments are confidential and intended solely "
    "for the addressee. If you have received this message in error, please "
    "notify the sender and delete it immediately."
)

MESSAGES = [
    dict(
        msg_key="<a1@acme.com>", conversation_id="c1",
        subject="Q3 numbers",
        sender_name="Jane Patel", sender_addr="jane.patel@acme.com",
        sent_time="2026-09-01T09:14:00Z", received_time="2026-09-01T09:14:22Z",
        unique_html=(
            "<div>Hi Carl,</div><p>Numbers look fine to me. Approving now.</p>"
            "<p>Jane</p>"
            # wrapped tight, one long line
            "<p>" + DISCLAIMER + "</p>"
        ),
        history_html="<blockquote><p>From: Carl<br>Can you check Q3?</p></blockquote>",
        to=[("carl.wei@acme.com", "Carl Wei")],
        cc=[("legal@acme.com", None)],
    ),
    dict(
        msg_key="<b2@acme.com>", conversation_id="c2",
        subject="NDA draft",
        sender_name="Sam Okafor", sender_addr="sam.okafor@acme.com",
        sent_time="2026-09-02T14:02:00Z", received_time="2026-09-02T14:02:40Z",
        unique_html=(
            "<div>Attached.</div>"
            # same footer, hard-wrapped at ~50 chars like Outlook mobile
            "<div>This e-mail and any attachments are<br>confidential and "
            "intended solely for the<br>addressee. If you have received this<br>"
            "message in error, please notify the<br>sender and delete it "
            "immediately.</div>"
        ),
        history_html="",
        to=[("carl.wei@acme.com", "Carl Wei")],
        cc=[],
    ),
    dict(
        msg_key="<c3@acme.com>", conversation_id="c3",
        subject="Re: vendor onboarding",
        sender_name="Ian Chen", sender_addr="ian.chen@acme.com",
        sent_time="2026-09-03T08:30:00Z", received_time="2026-09-03T08:30:11Z",
        unique_html=(
            "<p>Sounds good, looping in Carl.</p>"
            # non-breaking spaces, as Outlook loves to emit
            "<p>This&nbsp;e-mail and any&nbsp;attachments are confidential and "
            "intended&nbsp;solely for the addressee. If you have received this "
            "message in error, please notify the sender and delete it "
            "immediately.</p>"
            "<p>Sent from my iPhone</p>"
        ),
        history_html="<blockquote>long thread Carl was never on</blockquote>",
        to=[("carl.wei@acme.com", "Carl Wei"), ("ann.lee@acme.com", "Ann Lee")],
        cc=[],
    ),
]

PATTERNS = [
    ("Acme confidentiality footer", DISCLAIMER, "literal"),
    ("Mobile signature", "Sent from my iPhone", "literal"),
    ("Unused legacy footer", "Please consider the environment before printing", "literal"),
]


def main(path):
    db = sqlite3.connect(path)
    db.executescript(open("schema.sql").read())

    for m in MESSAGES:
        body_html = m["unique_html"] + m["history_html"]
        unique_text = outlook_reader.html_to_text(m["unique_html"])
        body_text = outlook_reader.html_to_text(body_html)
        db.execute(
            """INSERT OR REPLACE INTO messages
               (msg_key, conversation_id, subject, sender_name, sender_addr,
                sent_time, received_time, body_raw, body_type, unique_body_raw,
                content, quoted_history, first_ingested)
               VALUES (?,?,?,?,?,?,?,?,'html',?,?,?,datetime('now'))""",
            (m["msg_key"], m["conversation_id"], m["subject"], m["sender_name"],
             m["sender_addr"], m["sent_time"], m["received_time"], body_html,
             m["unique_html"], unique_text,
             outlook_reader.quoted_history(body_text, unique_text)),
        )
        rows = [(m["msg_key"], "from", m["sender_addr"].lower(), m["sender_name"])]
        rows += [(m["msg_key"], "to", a.lower(), n) for a, n in m["to"]]
        rows += [(m["msg_key"], "cc", a.lower(), n) for a, n in m["cc"]]
        db.executemany(
            "INSERT OR REPLACE INTO participants (msg_key, role, addr, name) VALUES (?,?,?,?)",
            rows,
        )

    for label, text, kind in PATTERNS:
        db.execute(
            """INSERT INTO disclaimer_patterns (label, pattern_text, kind, created_at)
               VALUES (?,?,?,datetime('now'))""",
            (label, text, kind),
        )
    db.commit()
    print(f"seeded {len(MESSAGES)} messages, {len(PATTERNS)} patterns -> {path}")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mail.db")
