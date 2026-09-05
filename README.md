# Outlook -> SQLite

Reads mail, stores it, strips boilerplate. Standard library only -- nothing to
install, which matters on a locked-down work machine.

## Files

| File | What it does |
|---|---|
| `schema.sql` | The database: `messages`, `participants`, `disclaimer_patterns`, `disclaimer_hits` |
| `textnorm.py` | HTML -> plain text, whitespace tidying |
| `clean.py` | Removes disclaimers. Re-runnable. |
| `demo_seed.py` | Fake mailbox so you can test without Outlook |

## Try it

```
python3 demo_seed.py mail.db
python3 clean.py mail.db
python3 clean.py mail.db --report
```

## The idea

Two stages, kept apart on purpose:

**Ingest** is slow, needs the network, and happens once per email. Columns
`body_raw` and `unique_body_raw` are written then and never touched again.

**Clean** is instant, local, and you will run it many times as you find new
boilerplate. `cleaned_content` can be deleted and rebuilt at any moment.

`cleaner_version` is a fingerprint of the code plus the exact set of enabled
patterns. Add, edit or disable a pattern and every row is automatically stale
on the next run -- there is no version number to remember to bump.

## Adding a disclaimer

```sql
INSERT INTO disclaimer_patterns (label, pattern_text, kind, created_at)
VALUES ('Vendor footer', 'This message is intended only for...', 'literal', datetime('now'));
```

Then `python3 clean.py mail.db`.

Paste the text as you see it on screen. Line breaks do not matter: literal
patterns match whitespace-blind, because the same footer arrives wrapped
differently from Outlook desktop, mobile and web. That one detail is the
difference between catching most disclaimers and catching almost none.

Use `kind='regex'` only when a literal will not do -- a footer with a changing
date or reference number.

## Reading the output

`--report` shows how many messages each pattern hit. A pattern showing
`never fires` is either wrong or no longer needed.

If a message cleans to empty the run says so by name. That usually means a
pattern is too broad.

## Not done yet

Fetching from Outlook. The `messages` and `participants` tables are the target
shape; something needs to fill them.
