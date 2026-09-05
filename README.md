# Outlook -> SQLite

Reads mail, splits each message from the chain quoted below it, strips
boilerplate, stores the result. Standard library only -- nothing to install,
which matters on a locked-down work machine.

```
python3 cli.py mail.db init      # create tables, seed default patterns
python3 cli.py mail.db demo      # load a fake mailbox to try it on
python3 cli.py mail.db run       # split, then clean
python3 cli.py mail.db report    # what fired, what never fires
python3 tests.py                 # 44 assertions
```

## Files

| File | Job |
|---|---|
| `schema.sql` | The six tables |
| `db.py` | Open, create schema, seed patterns, store a message |
| `textnorm.py` | HTML -> text, whitespace normalisation |
| `splitter.py` | Find where the quoted chain begins |
| `cleaner.py` | Remove disclaimers |
| `cli.py` | One entry point for every command |
| `fetch.py` | **Stub.** Pull mail from Outlook -- the one unwritten half |
| `demo.py` | Fake mailbox with real-world marker formats |
| `tests.py` | Assertions |
| `SPLITTER.md` | Why the splitter works the way it does |

## Pipeline

```
fetch  ->  split  ->  clean
```

**fetch** is slow, needs the network or a running Outlook, and happens once per
message. It writes `body_raw` and nothing else derived.

**split** cuts `body_raw` at the first quoted-chain marker into `content` (what
this sender newly wrote) and `quoted_history`.

**clean** removes disclaimers from `content`, producing `cleaned_content`.

Raw columns are written once and never modified. Everything else is derived and
can be deleted and rebuilt at any time -- which you will do often, as you find
new marker formats and new boilerplate.

Order matters: a disclaimer sits inside the sender's own text, so cleaning runs
on `content` after the split, not on the raw body. Re-splitting therefore
invalidates cleaning automatically.

## Staleness handles itself

`splitter_version` and `cleaner_version` each store a fingerprint of the code
plus the exact set of enabled patterns. Add, edit or disable a pattern and every
affected message stops matching its stored fingerprint, so the next run redoes
it. There is no version number to remember to bump and no refresh flag to
remember to pass.

## Tables

| Table | One row is |
|---|---|
| `messages` | one email actually delivered to the mailbox |
| `participants` | one person on one email, with their role |
| `boundary_patterns` | one quoted-chain marker format |
| `disclaimer_patterns` | one piece of boilerplate to remove |
| `disclaimer_hits` | one disclaimer removed from one message |
| `sync_state` | how far the last fetch got |

Recipients get their own table because an email has many of them. Everything
else follows from that: `SELECT ... WHERE role='cc' AND addr='legal@acme.com'`
is exact, indexed, and countable, where searching a comma-joined string is none
of those.

## Adding a quoted-chain marker

When you see a format that is not being caught, add a row:

```sql
INSERT INTO boundary_patterns (label, line_regex, require_regex, example_block)
VALUES ('Some client',
        '^\s*On\b.*\bwrote:\s*$',
        '<[^<>@\s]+@[^<>\s]+>',
        'On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:');
```

Three fields do three different jobs:

- `line_regex` -- the frame. Loose on purpose: dates, names and credentials
  vary far too much to parse.
- `require_regex` -- must also match the *same* line. Without requiring an
  address, the frame above also matches the sentence
  "On the topic of the memo he wrote:".
- `confirm_regex` -- must match a *following* line. `From:` alone is common in
  ordinary prose; `From:` followed by `Sent:` is not.

`example_block` is required, and the pattern must cut it at line 0.
`python3 cli.py mail.db test-patterns` enforces that. A regex that looks correct
but matches nothing is otherwise invisible -- it simply never fires.

Then `python3 cli.py mail.db run`. Every message re-splits automatically.

## Adding a disclaimer

```sql
INSERT INTO disclaimer_patterns (label, pattern_text, kind, created_at)
VALUES ('Vendor footer', 'This message is intended only for...', 'literal',
        datetime('now'));
```

Paste the text as it appears on screen. Line breaks do not matter: literal
patterns match whitespace-blind, because the same footer arrives wrapped
differently from Outlook desktop, mobile and web. That single detail is the
difference between catching most disclaimers and catching almost none.

Use `kind='regex'` only when a literal cannot work -- a footer carrying a
changing date or reference number.

## Reading the report

- A pattern showing `never fires` is wrong, or that format never arrives.
- The count of messages with **no boundary found** is the number that matters.
  `python3 cli.py mail.db unsplit` prints a sample; each distinct marker you
  see there is one more row to add.
- A message that cleans to **empty** is named during the run. That almost
  always means a disclaimer pattern is too broad.

## Not done yet

`fetch.py`. Its docstring gives the exact contract, plus the traps on each
route -- Graph needs an app registration; classic Outlook returns Exchange
directory paths instead of email addresses, and its EntryID changes when a
message moves folders.
