"""Pull mail from Outlook into the database.

NOT IMPLEMENTED -- this is the one half that depends on which route you get.
Everything downstream of it (split, clean, report) is written and tested.

The contract, whichever route you take:

    for each message:
        db.store_message(conn, {
            "msg_key":        <internetMessageId, or PR_INTERNET_MESSAGE_ID>,
            "conversation_id": ...,
            "subject": ..., "sender_name": ..., "sender_addr": ...,
            "sent_time": <ISO8601 UTC>, "received_time": <ISO8601 UTC>,
            "folder": ..., "has_attachments": 0/1,
            "body_raw":  <the WHOLE body, quoted chain included>,
            "body_type": "html" or "text",
        }, participants=[("to", addr, name), ("cc", addr, name), ...])

Write RAW columns only. Leave content / quoted_history / cleaned_content NULL
-- the splitter and cleaner fill them, and being NULL is what marks a message
as needing work.

Route A -- Microsoft Graph (preferred)
    Needs an app registration with delegated Mail.Read. Ask for text bodies
    (Prefer: outlook.body-content-type="text") and set body_type="text";
    that drops the HTML conversion entirely.
    Graph also offers `uniqueBody`, which strips the quoted chain server-side.
    If you have it, store it as `content` directly and skip the splitter --
    but keep storing the full `body` as body_raw either way.
    Use the delta endpoint to discover changes, storing the deltaLink in
    sync_state, then fetch each new message individually.

Route B -- classic Outlook on Windows (no IT approval needed)
    No uniqueBody: every body arrives as a full chain, which is exactly what
    splitter.py is for.
    Two traps:
      * MailItem.SenderEmailAddress returns an Exchange directory path
        (/O=EXCHANGE/OU=.../CN=JSMITH) for internal senders, not an SMTP
        address. Resolve it via Sender.GetExchangeUser().PrimarySmtpAddress,
        or PropertyAccessor on PR_SMTP_ADDRESS. Same for each Recipient.
      * EntryID changes when a message moves folders, so it cannot be
        msg_key. Use PR_INTERNET_MESSAGE_ID
        (http://schemas.microsoft.com/mapi/proptag/0x1035001F).
    Outlook must be running, and every property read is a round trip, so
    fetch in one pass and store as you go.
"""


def fetch_messages(conn, **kwargs):
    raise NotImplementedError(
        "fetch is not written yet -- see the module docstring for the contract"
    )
