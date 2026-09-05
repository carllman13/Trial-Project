"""Pull mail from classic Outlook on Windows into the database.

NOT IMPLEMENTED -- this is the one half still to write. Everything downstream
of it (split, clean, report) is written and tested.

Route: the Outlook desktop application's automation interface, via pywin32.
Outlook must be installed, running and signed in, and this only runs on
Windows. Nothing needs approving by IT.

The contract
------------

    import db
    for each mail item:
        db.store_message(conn, {
            "msg_key":         <PR_INTERNET_MESSAGE_ID -- see below>,
            "conversation_id": item.ConversationID,
            "subject":         item.Subject,
            "sender_name":     item.SenderName,
            "sender_addr":     <resolved SMTP address -- see below>,
            "sent_time":       <ISO8601 UTC>,
            "received_time":   <ISO8601 UTC>,
            "folder":          folder.FolderPath,
            "has_attachments": 1 if item.Attachments.Count else 0,
            "body_raw":        item.HTMLBody,   # the WHOLE body, chain included
            "body_type":       "html",
        }, participants=[("to", addr, name), ("cc", addr, name), ...])

Write RAW columns only. Leave content / quoted_history / cleaned_content NULL:
the splitter and cleaner fill them, and being NULL is what marks a message as
needing work.

Four traps, in the order they will bite
---------------------------------------

1.  EntryID is not a stable key. It changes when a message moves folders, so
    a message filed into a subfolder would be stored twice. Use the internet
    message id instead, read through PropertyAccessor:

        TAG = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
        msg_key = item.PropertyAccessor.GetProperty(TAG)

    A few items genuinely lack it (drafts, some calendar items). Skip those,
    or synthesise a key and record that you did.

2.  SenderEmailAddress is not an email address for internal senders. Exchange
    returns a directory path such as

        /O=EXCHANGE/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=JSMITH

    Resolve it, and fall back where resolution fails:

        if item.SenderEmailType == "EX":
            addr = item.Sender.GetExchangeUser().PrimarySmtpAddress
        else:
            addr = item.SenderEmailAddress

    The same applies to every entry in item.Recipients -- use
    recipient.AddressEntry.GetExchangeUser().PrimarySmtpAddress, guarded, since
    GetExchangeUser() returns None for external and for distribution lists.
    Getting this wrong fills sender_addr and participants.addr with directory
    paths, and every query by address silently returns nothing.

3.  Recipient roles come as numbers. recipient.Type is 1=to, 2=cc, 3=bcc.
    Map them to the 'to'/'cc'/'bcc' the participants table expects.

4.  Every property read is a round trip to the Outlook process. Reading a few
    fields from 40,000 items one at a time is slow enough to feel broken. Pull
    what you need in a single pass per item and store as you go, rather than
    holding items and re-reading them later.

Incremental runs
----------------

Keep a high-water mark per folder in sync_state, and restrict on it rather
than walking everything:

    items = folder.Items
    items.Sort("[ReceivedTime]", True)
    items = items.Restrict("[ReceivedTime] >= '" + last_run_local + "'")

Outlook's Restrict filter wants a locale-formatted date string, not ISO, and
it compares in local time. Overlap the window by a day and rely on
msg_key being the primary key to absorb the duplicates -- cheaper than getting
the boundary exactly right.

Times come back as local, timezone-naive. Convert to UTC before storing, or
sent_time comparisons across a DST change will be wrong.
"""


def fetch_messages(conn, **kwargs):
    raise NotImplementedError(
        "fetch is not written yet -- see the module docstring for the contract"
    )
