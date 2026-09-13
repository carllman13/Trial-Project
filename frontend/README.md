# Local Outlook Digest frontend

Three screenshot-based screens: **Folders**, **Database**, **Disclaimer list**.
Plain HTML, CSS and JavaScript. No packages, build step, CDN or network dependency.
The existing Python backend is unchanged. The fourth navigation label, Drop .msg,
is visible but disabled because that screen was not requested.

## Open on the work computer

Open `frontend/index.html` in Edge or Chrome, or serve this folder using your
existing local web server. Open at 1920-pixel viewport width and 100% browser zoom
to compare with the supplied screenshots. Smaller windows preserve the dense
desktop layout and may scroll horizontally.

All included email content is synthetic. Layout, colours and controls follow the
screenshots; this is not a copy of the confidential underlying email dataset.
No application or automated tests were run on the development computer, as
requested. Exact pixel matching still needs a browser check on the work computer.

## Five files, three responsibilities

| File | Responsibility |
| --- | --- |
| `index.html` | Page shell and script loading |
| `styles.css` | Screenshot-style layout and appearance |
| `app.js` | Rendering and browser interactions |
| `sample-data.js` | Synthetic preview data, separate from UI logic |
| `README.md` | Setup and backend handoff |

Implemented browser interactions: tab switching, folder selection and scan
checkboxes, table sorting, Ctrl/Cmd-click multi-selection, chain search, message
filters, dragging rows into queues, removing/clearing queued items, copying
messages/chains/prompts, showing raw message text, editing/toggling disclaimers.
Filter criteria survive detail selection and switching tabs. Last N days in the
preview is anchored to the sample snapshot date. Date filters use London dates;
the evening window is 18:00 inclusive to 22:00 exclusive.

Preview changes last only until the page reloads. No data is saved in browser
storage. Clipboard availability depends on browser policy; a fallback supports
local-file previews where permitted. Raw HTML is displayed as **text**, never
executed or inserted as message markup.

## Connect the work backend later

Define `window.OutlookDigestAdapter` in a script loaded before `app.js` (the HTML
marks the insertion point). The adapter can call the existing local server's
endpoints. Do not put Outlook access, SQL or disclaimer matching in `app.js`.
No particular endpoint paths or Python web framework are assumed.

Methods may return values directly or promises:

| Method | Input | Return |
| --- | --- | --- |
| `getInitialData()` | None | Complete data object described below; required |
| `queryMessages(filters)` | Filter form values | Message array |
| `refresh(options)` | `mode`: `last`, `cutoff` or `start`; selected `folders`; `cutoff: {days,hours,minutes}` | Complete updated data object |
| `refreshFolders()` | None | Folder name array |
| `saveDisclaimers(settings)` | `{disclaimers, autoClean}` | Resolve on success; throw on failure |
| `backfill(settings)` | `{disclaimers, autoClean}` | Resolve on completion; throw on failure |

An absent method produces a “backend is not connected” notice. Nothing pretends
to refresh Outlook, save settings or clean the database. If initial loading fails,
the UI shows the error instead of silently falling back to sample emails.
The backfill adapter owns saving/using the submitted settings and reporting any
long-running job progress. Auto-clean changes only become persistent when saved.

### Data contract

Use `sample-data.js` as the concrete example. A complete data object has:

```js
{
  account: 'Local mailbox',
  lastSynced: '2026-09-11T12:40:06', // display label
  totalRows: 6644,
  folders: ['Humain'],             // use full unique paths for real folders
  messages: [{
    id: 'stable-message-id', chainId: 'stable-chain-id', folder: 'Humain',
    received: '2026-09-11T11:01:00Z', // ISO timestamp with timezone
    from: 'Sender <sender@example.com>', to: 'recipient@example.com', cc: '',
    subject: 'Example', attachments: 0,
    body: 'Extracted message text', rawBody: '<html>original source</html>',
    lastSeen: ''
  }],
  chains: [{
    id: 'stable-chain-id', folder: 'Humain', count: 1,
    received: '2026-09-11T11:01:00Z', subject: 'Example',
    from: 'sender@example.com', to: 'recipient@example.com',
    body: 'Full chain text supplied by the backend'
  }],
  prompts: [{title: 'Summary', text: 'Summarize this discussion.'}],
  disclaimers: [{text: 'Exact disclaimer text', enabled: true}],
  autoClean: false
}
```

The backend supplies chain membership and text: the frontend does **not** split,
deduplicate, clean or reconstruct emails. Provide the chains referenced by query
results in the loaded data; otherwise those rows cannot be queued as full chains.
For a large mailbox, replace this eager chain loading with an adapter fetch method
when needed; that optimization is deliberately outside this frontend replica.
Recipients are formatted display strings, not a replacement for backend recipient
records. Prompts are editable session drafts; persistent prompt settings can be
added at the adapter boundary later.

Query filter keys: `folder`, `fromDate`, `toDate`, `days`, `from`, `to`, `includeCC`,
`cc`, `subject`, `body`, `limit`, `evenings`. Checkboxes are booleans; other values
are strings. Validate them in the backend. This frontend never interpolates them
into SQL.

## Work-computer check

1. Open the page and compare all three tabs against the screenshots.
2. Select rows (Ctrl-click for several), drag to each matching queue, copy and
   paste into a local text editor. Confirm counts and contents.
3. Query a subject, sort a column, select a result, and verify filters remain.
4. Edit/add/disable a disclaimer. Reload to confirm preview edits are not saved.
5. Confirm refresh/backfill show “not connected” until an adapter is supplied.

For visual feedback, one screenshot per tab at the same zoom is enough. Avoid
sharing real email content; keep the sample data when reporting layout issues.
