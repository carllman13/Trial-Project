# Outlook -> SQLite

Reads mail, splits each message from the chain quoted below it, strips
boilerplate, stores the result. Standard library only -- nothing to install,
which matters on a locked-down work machine.

```
python3 cli.py mail.db init                    # create tables, seed patterns
python3 cli.py mail.db fetch --since-days 30   # read from classic Outlook
python3 cli.py mail.db run                     # split, then clean
python3 cli.py mail.db report                  # what fired, what never fires
python3 tests.py                               # 92 assertions
```

Or without Outlook, to see it work: `python3 cli.py mail.db demo` then `run`.

`fetch` needs Windows, classic Outlook installed **and running with a
logged-in profile**, reach to the company directory, and `pip install
pywin32`. Resolving an address is a directory lookup, not a read of the
message, so all four are required -- it cannot run on Linux or in a container.
Programmatic address access can also raise a security prompt or be blocked by
group policy, so test on the managed work laptop rather than a personal
machine.

Everything after `fetch` is plain Python and SQLite, and runs anywhere.

## Files

| File | Job |
|---|---|
| `schema.sql` | The six tables |
| `db.py` | Open, create schema, seed patterns, store a message |
| `textnorm.py` | HTML -> text, whitespace normalisation |
| `splitter.py` | Find where the quoted chain begins |
| `cleaner.py` | Remove disclaimers |
| `cli.py` | One entry point for every command |
| `fetch.py` | Pull mail from classic Outlook via its automation interface |
| `demo.py` | Fake mailbox with real-world marker formats |
| `tests.py` | Assertions |
| `SPLITTER.md` | Why the splitter works the way it does |

## Pipeline

```
fetch  ->  split  ->  clean
```

**fetch** needs a running Outlook and happens once per message. It writes
`body_raw` and nothing else derived.

**split** cuts `body_raw` at the first quoted-chain marker and keeps what is
above it as `content` -- what this sender newly wrote. The chain below is
dropped, since `body_raw` still holds it untouched.

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
- The **ADDRESSES** section counts senders Outlook would not resolve and
  recipients that had to be dropped. `list-unresolved` shows which messages,
  with the sender's display name, which is recorded even when the address is
  not -- so you can still tell who they were. These are usually people who
  have left the company, and only affect mail fetched after they left: a row
  already in your database keeps its address permanently.

## Fetching, and what Outlook does to you

`fetch.py` handles four traps. Worth knowing they exist, because the symptoms
are all silent:

**Addresses are not addresses.** For internal senders Exchange returns
`/O=EXCHANGE/OU=.../CN=JSMITH`, not `jane.patel@acme.com`. Resolution is tried
three ways -- the SMTP property, `GetExchangeUser()`, then the raw field --
because each fails on a different kind of recipient, and distribution lists
need `GetExchangeDistributionList()` instead. Anything still unresolved is
stored as NULL rather than as a directory path, so a query by address never
half-matches junk.

**`EntryID` changes when a message moves folders**, so it cannot be the key.
The internet message id is read through `PropertyAccessor` instead. Items
without one (drafts, some calendar items) are skipped and counted.

**Every address lookup is a directory round trip.** The same colleagues recur
on message after message, so results are cached for the run -- failures
included, since someone who has left will not resolve on a retry either.
Without it a large mailbox takes hours. The entry's type is also checked
before choosing which lookup to make, rather than trying both and catching the
throw.

**Times come back naive and local.** They are read as local and converted to
UTC. Treating them as UTC would shift every timestamp, and by an extra hour
across a DST boundary.

**Outlook's `Restrict` filter wants a locale-formatted local-time string**, not
ISO. On a machine with non-US regional settings it can match nothing at all
and look like an empty mailbox -- `--no-restrict` filters in Python instead,
slower but immune.

A single item that Outlook refuses to hand over is logged and skipped rather
than ending the run; re-running is always safe, since `msg_key` is the primary
key.

`participants` describes the **top message only** -- the people Outlook lists
on the item you received. Anyone appearing solely inside the quoted chain
below is not in that table. That is deliberate: those are paragraphs inside a
body, not deliveries to your mailbox.

Only RAW columns are written. `content` and `cleaned_content` are left NULL,
which is what marks a message as needing work.

### Untested against a real mailbox

Everything above is exercised against stubs that mimic Outlook's behaviour,
including its failures. None of it has run against real Outlook -- that needs
Windows. Expect to shake something out on the first run; start with
`--limit 20` and read what lands.
