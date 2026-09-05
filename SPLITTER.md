# Reply splitter — spec

## The job

For one message body, find **where the quoted part begins**. Everything above
that point is what this sender newly wrote; everything from that point down is
history.

Only the **first** boundary matters. We are not breaking a thread into N
messages -- just cutting it in two.

    content         = body above the first boundary
    quoted_history  = body from the boundary down

If no boundary is found, the whole body is `content` and the message is
recorded as unsplit. That is a normal outcome, not an error -- the list of
unsplit messages is the to-do list of formats still to add.

## The principle: match the frame, not the contents

A boundary marker looks like this, and varies enormously:

    On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:
    On Sun, Aug 9, 2026 at 8:49 PM David Salfati <ds.ext@blackfuel.ai> wrote:
    On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain CFA, CAIA <BAlhussain@humain.com> wrote:

Dates differ (weekday or not, `7 Aug` or `Aug 9`, 24h or AM/PM, seconds,
timezone). Names contain commas (`Khalil, Adam`) and credentials
(`CFA, CAIA`). Any regex that tries to *parse* those fields will be long,
brittle, and will need editing for every new sender.

Do not parse the line. Only recognise it:

    ^\s*On\b.*\bwrote:\s*$

That matches all three. The variable middle is skipped over, not understood.

Pull attribution out separately and opportunistically -- an email address is
easy and reliable, a date is not:

    <([^<>@\s]+@[^<>\s]+)>

If attribution extraction fails, the split is still valid. Never let it block.

## Whitespace and wrapping

This is the same failure that broke the disclaimer matcher, and it will break
this if ignored. HTML-to-text conversion hard-wraps long lines, so a marker
over ~76 characters arrives split across two lines:

    On September 4, 2026 at 10:08:08 PM GMT+5, Bader Al Hussain CFA,
    CAIA <BAlhussain@humain.com> wrote:

Matching strictly line-by-line misses it, because `wrote:` is not on the line
that starts with `On`.

**Rule:** at each line `i`, try progressively longer windows -- line `i`
alone, then `i`+1, then `i`+2 -- collapsing whitespace runs to single spaces,
and take the first window that matches. Record the boundary at line `i`.

The progressive part is load-bearing. A fixed 3-line join breaks the `$`
anchor whenever the marker is *not* wrapped and ordinary body text follows it
on the next line: the window becomes `"...wrote: > old stuff"`, which no
longer ends at `wrote:`. Trying the 1-line window first matches unwrapped
markers cleanly; the 2- and 3-line windows catch the wrapped ones.

Every pattern is matched case-insensitively against the collapsed form.

## Pattern storage

Patterns live in the database, never in code. Adding a format is one INSERT.

```sql
CREATE TABLE boundary_patterns (
    pattern_id      INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,      -- 'Gmail / Apple On-wrote'
    line_regex      TEXT NOT NULL,      -- must match the logical line
    require_regex   TEXT,               -- optional; must ALSO match same logical line
    confirm_regex   TEXT,               -- optional; must match a later line
    confirm_within  INTEGER DEFAULT 5,  -- how many lines ahead to look
    priority        INTEGER DEFAULT 100,-- tie-break only; earliest line always wins
    enabled         INTEGER NOT NULL DEFAULT 1,
    example_line    TEXT NOT NULL,      -- a real marker this pattern must match
    notes           TEXT
);
```

Three separate conditions, because they do different work:

- `line_regex` — the frame. Loose on purpose.
- `require_regex` — a discriminator on the *same* line. `^\s*On\b.*\bwrote:\s*$`
  alone also matches the sentence "On the topic of the memo he wrote:";
  requiring an email address on the line removes that without tightening the
  frame.
- `confirm_regex` — a discriminator on a *following* line. Needed for the
  Outlook block form, where `From:` alone is far too common in ordinary text
  but `From:` followed by `Sent:` within a few lines is not.

`example_line` is mandatory. See "Self-test" below.

## Seed patterns

| label | line_regex | require_regex | confirm_regex |
|---|---|---|---|
| Gmail / Apple On-wrote | `^\s*On\b.*\bwrote:\s*$` | `<[^<>@\s]+@[^<>\s]+>` | |
| Outlook header block (EN) | `^\s*From:\s*\S` | | `^\s*(Sent\|Date):` |
| Outlook header block (FR) | `^\s*De\s*:\s*\S` | | `^\s*(Envoyé\|Date)\s*:` |
| Outlook header block (DE) | `^\s*Von:\s*\S` | | `^\s*Gesendet:` |
| Original message marker | `^\s*-+\s*Original Message\s*-+\s*$` | | |
| Forwarded message marker | `^\s*-+\s*Forwarded message\s*-+\s*$` | | |
| Quote prefix run | `^\s*>` | | `^\s*>` (within 2) |

French and German rows are guesses at the localised forms -- verify against
real mail before trusting them. A wrong pattern that never fires is visible in
the report; a missing one is not.

## Algorithm

```
MAX_JOIN = 3

split(body, patterns):
    lines = body.split("\n")

    for i in range(len(lines)):                   # earliest line wins
        for span in range(1, MAX_JOIN + 1):       # 1 line, then 2, then 3
            logical = collapse_ws(" ".join(lines[i : i+span]))
            if not logical:
                continue
            for p in sorted(patterns, key=lambda p: p.priority):
                if not p.line_regex.search(logical):
                    continue
                if p.require_regex and not p.require_regex.search(logical):
                    continue
                if p.confirm_regex:
                    window = lines[i+span : i+span+p.confirm_within]
                    if not any(p.confirm_regex.search(l) for l in window):
                        continue
                return ("\n".join(lines[:i]),    # content
                        "\n".join(lines[i:]),    # quoted_history
                        p.pattern_id)

    return body, "", None                          # no boundary found
```

Scanning top-down and returning on the first hit is what makes "earliest
boundary wins" fall out for free. `priority` only orders patterns *within* one
position, for the rare case where two match the same line.

Normalise both halves afterwards (the existing `normalize()`).

Earliest match always wins -- that is the direct expression of "a message ends
where the next one begins". `priority` only breaks ties when two patterns match
the same line.

## Storage

Add to `messages`:

```sql
content              TEXT     -- above the boundary          [derived]
quoted_history       TEXT     -- from the boundary down      [derived]
boundary_pattern_id  INTEGER  -- which pattern cut it; NULL = unsplit
splitter_version     INTEGER  -- fingerprint, see below
```

`body_raw` is never modified. Everything above is derived and rebuilt on
demand, exactly like `cleaned_content`.

`splitter_version` is a fingerprint of the splitter code version plus the exact
set of enabled patterns (same approach as `cleaner_fingerprint`). Add, edit or
disable a pattern and every message goes stale automatically and re-splits on
the next run. Nothing to remember to bump.

**Order matters:** split first, then clean. A disclaimer sits inside the new
text; run the cleaner on `content` after the split, not on `body_raw`.

## Adding a pattern

One INSERT, with a real example line:

```sql
INSERT INTO boundary_patterns (label, line_regex, require_regex, example_line)
VALUES ('Outlook mobile',
        '^\s*On\b.*\bwrote:\s*$',
        '<[^<>@\s]+@[^<>\s]+>',
        'On Fri, 7 Aug 2026 at 20:39, Khalil, Adam <Adam.Khalil@gs.com> wrote:');
```

Then re-run the splitter. Every message re-splits automatically.

## Self-test

Provide a `--test-patterns` command that, for every enabled row, checks the
pattern actually matches its own `example_line` (and its `require_regex` too).
Report any that fail.

This exists because new patterns will arrive written by another AI from a
screenshot. A regex that looks right and matches nothing is otherwise
invisible -- it just quietly never fires. The self-test turns that into an
immediate, named failure.

Reject a pattern at INSERT time if it fails its own example.

## Diagnostics

Provide a report showing, per pattern: how many messages it cut, and a count of
messages with no boundary found.

- A pattern with **zero** hits is wrong, or that format never arrives.
- The **unsplit** count is the number that matters. Read a sample of those
  bodies; each distinct marker you see is one new row.

## Known limits — accept these, do not engineer around them

1. **A quoted marker inside genuine content.** If someone pastes an email into
   their message to discuss it, the split cuts early and real content lands in
   `quoted_history`. `body_raw` is intact, so it is recoverable.
2. **No marker at all.** Some clients quote with nothing but indentation.
   Those stay unsplit, which is the correct outcome.
3. **Attribution is best-effort.** Address usually available, date often not.
4. **The boundary line itself** goes to `quoted_history`, not `content`.
