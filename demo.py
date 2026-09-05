"""A fake mailbox, so the pipeline can be exercised without Outlook.

Marker formats are copied verbatim from real emails, including their awkward
parts: commas inside names (`Khalil, Adam`), credentials (`CFA, CAIA`), four
different date formats, and one marker long enough to be wrapped by the
HTML-to-text conversion.
"""
import cleaner
import db as dbmod

DISCLAIMER = (
    "This e-mail and any attachments are confidential and intended solely for "
    "the addressee. If you have received this message in error, please notify "
    "the sender and delete it immediately."
)

MESSAGES = [
    # 1. Outlook's own header block, the default corporate reply shape.
    dict(
        msg_key="<a1@acme.com>", conversation_id="c1", subject="RE: Q3 numbers",
        sender_name="Jane Patel", sender_addr="jane.patel@acme.com",
        sent_time="2026-09-01T09:14:00Z", received_time="2026-09-01T09:14:22Z",
        folder="Inbox",
        body_raw=(
            "<div>Hi Carl,</div><p>Numbers look fine to me. Approving now.</p>"
            "<p>Jane</p>"
            "<p>" + DISCLAIMER + "</p>"
            "<div>________________________________</div>"
            "<div>From: Carl Wei &lt;carl.wei@acme.com&gt;</div>"
            "<div>Sent: 31 August 2026 17:40</div>"
            "<div>To: Jane Patel</div>"
            "<div>Subject: Q3 numbers</div>"
            "<p>Can you check the Q3 figures before Friday?</p>"
        ),
        participants=[("from", "jane.patel@acme.com", "Jane Patel"),
                      ("to", "carl.wei@acme.com", "Carl Wei"),
                      ("cc", "legal@acme.com", None)],
    ),
    # 2. Gmail-style marker, disclaimer hard-wrapped as Outlook mobile sends it.
    dict(
        msg_key="<b2@gs.com>", conversation_id="c2", subject="Re: comps",
        sender_name="David Salfati", sender_addr="ds.ext@blackfuel.ai",
        sent_time="2026-08-09T20:49:00Z", received_time="2026-08-09T20:49:30Z",
        folder="Inbox",
        body_raw=(
            "<div>Thanks David - great to connect earlier. We are working on "
            "the comps and will revert.</div>"
            "<div>This e-mail and any attachments are<br>confidential and "
            "intended solely for the<br>addressee. If you have received this<br>"
            "message in error, please notify the<br>sender and delete it "
            "immediately.</div>"
            "<div>On Sun, Aug 9, 2026 at 8:49 PM David Salfati "
            "&lt;ds.ext@blackfuel.ai&gt; wrote:</div>"
            "<div>&gt; Many thanks Carl, that's very helpful.</div>"
        ),
        participants=[("from", "ds.ext@blackfuel.ai", "David Salfati"),
                      ("to", "carl.wei@acme.com", "Carl Wei")],
    ),
    # 3. Marker long enough that HTML-to-text wraps it across two lines.
    dict(
        msg_key="<c3@humain.com>", conversation_id="c3", subject="Re: onboarding",
        sender_name="Bader Al Hussain", sender_addr="balhussain@humain.com",
        sent_time="2026-09-04T22:08:08Z", received_time="2026-09-04T22:08:40Z",
        folder="Inbox",
        body_raw=(
            "<p>Sounds good, looping in Carl.</p>"
            "<p>Sent from my iPhone</p>"
            "<div>On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain "
            "CFA, CAIA<br>&lt;BAlhussain@humain.com&gt; wrote:</div>"
            "<div>Earlier discussion nobody needs.</div>"
        ),
        participants=[("from", "balhussain@humain.com", "Bader Al Hussain"),
                      ("to", "carl.wei@acme.com", "Carl Wei"),
                      ("cc", "ann.lee@acme.com", "Ann Lee")],
    ),
    # 4. Non-breaking spaces in the disclaimer, and no quoted chain at all.
    dict(
        msg_key="<d4@acme.com>", conversation_id="c4", subject="NDA draft",
        sender_name="Sam Okafor", sender_addr="sam.okafor@acme.com",
        sent_time="2026-09-02T14:02:00Z", received_time="2026-09-02T14:02:40Z",
        folder="Inbox",
        body_raw=(
            "<div>Attached.</div>"
            "<p>This&nbsp;e-mail and any&nbsp;attachments are confidential and "
            "intended&nbsp;solely for the addressee. If you have received this "
            "message in error, please notify the sender and delete it "
            "immediately.</p>"
        ),
        participants=[("from", "sam.okafor@acme.com", "Sam Okafor"),
                      ("to", "carl.wei@acme.com", "Carl Wei")],
    ),
    # 5. Nothing to split and nothing to clean -- the quiet case.
    dict(
        msg_key="<e5@acme.com>", conversation_id="c5", subject="Lunch",
        sender_name="Ann Lee", sender_addr="ann.lee@acme.com",
        sent_time="2026-09-03T11:00:00Z", received_time="2026-09-03T11:00:05Z",
        folder="Inbox",
        body_raw="<p>Canteen at 12:30?</p>",
        participants=[("from", "ann.lee@acme.com", "Ann Lee"),
                      ("to", "carl.wei@acme.com", "Carl Wei")],
    ),
]


def load(conn, log=print):
    for m in MESSAGES:
        m = dict(m)
        parts = m.pop("participants")
        dbmod.store_message(conn, m, parts)
    try:
        cleaner.add_pattern(conn, "Acme confidentiality footer", DISCLAIMER)
    except Exception:
        pass                       # already present from a previous demo load
    conn.commit()
    log(f"loaded {len(MESSAGES)} demo message(s)")
