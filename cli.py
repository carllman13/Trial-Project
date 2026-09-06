"""One entry point for the whole pipeline.

    python3 cli.py mail.db init            create tables, seed default patterns
    python3 cli.py mail.db folders         show the folder tree and what is ticked
    python3 cli.py mail.db folders --rebuild        re-read the tree from Outlook
    python3 cli.py mail.db select "Mailbox - Inbox" --recursive
    python3 cli.py mail.db refresh --hours 24       scan ticked folders
    python3 cli.py mail.db refresh --since-last     resume each folder
    python3 cli.py mail.db refresh --full           everything, no cutoff
    python3 cli.py mail.db demo            load a fake mailbox to try things on
    python3 cli.py mail.db split           find quoted chains
    python3 cli.py mail.db clean           strip disclaimers
    python3 cli.py mail.db run             split then clean
    python3 cli.py mail.db report          what fired, what did not
    python3 cli.py mail.db test-patterns   check patterns match their examples
    python3 cli.py mail.db unsplit         sample messages with no boundary
    python3 cli.py mail.db list-unresolved messages with unresolved addresses

Add --rebuild to redo every row, --dry-run to change nothing.
"""
import argparse
import textwrap

import cleaner
import db as dbmod
import folders
import refresh
import splitter


def cmd_report(conn, log=print):
    log("BOUNDARY PATTERNS")
    log(f"  {'pattern':<38} {'msgs':>6}")
    log("  " + "-" * 46)
    for label, n in conn.execute(
        "SELECT p.label, COUNT(m.msg_key) FROM boundary_patterns p "
        "LEFT JOIN messages m ON m.boundary_pattern_id = p.pattern_id "
        "WHERE p.enabled = 1 GROUP BY p.pattern_id ORDER BY COUNT(m.msg_key) DESC"
    ):
        log(f"  {label[:38]:<38} {n:>6}" + ("   <- never fires" if not n else ""))

    total, unsplit = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN boundary_pattern_id IS NULL THEN 1 ELSE 0 END) "
        "FROM messages WHERE boundary_patterns_version IS NOT NULL"
    ).fetchone()
    if total:
        log(f"\n  {unsplit or 0} of {total} split messages found no boundary")

    total, no_sender, dropped, with_dropped = conn.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN sender_addr IS NULL THEN 1 ELSE 0 END), "
        "       COALESCE(SUM(recipients_dropped), 0), "
        "       SUM(CASE WHEN recipients_dropped > 0 THEN 1 ELSE 0 END) "
        "FROM messages").fetchone()
    if total:
        log("\nADDRESSES")
        log(f"  messages with no sender address     {no_sender or 0:>6} of {total}")
        log(f"  recipients Outlook would not resolve {dropped or 0:>5}"
            f"  (across {with_dropped or 0} message(s))")
        if no_sender or dropped:
            log("  these are usually senders who have since left the company;")
            log("  `list-unresolved` shows which messages they are")

    log("\nDISCLAIMER PATTERNS")
    log(f"  {'pattern':<38} {'msgs':>6} {'chars':>8}")
    log("  " + "-" * 55)
    for label, n, chars in conn.execute(
        "SELECT p.label, COUNT(h.msg_key), COALESCE(SUM(h.chars_removed), 0) "
        "FROM disclaimer_patterns p "
        "LEFT JOIN disclaimer_hits h ON h.pattern_id = p.pattern_id "
        "WHERE p.enabled = 1 GROUP BY p.pattern_id ORDER BY COUNT(h.msg_key) DESC"
    ):
        log(f"  {label[:38]:<38} {n:>6} {chars:>8}"
            + ("   <- never fires" if not n else ""))


def cmd_unsplit(conn, limit=5, log=print):
    """Show messages no pattern matched. Each distinct marker is one new row."""
    rows = conn.execute(
        "SELECT msg_key, subject, content FROM messages "
        "WHERE boundary_patterns_version IS NOT NULL AND boundary_pattern_id IS NULL "
        "LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        log("every split message found a boundary")
        return
    log(f"{len(rows)} message(s) with no boundary found:\n")
    for msg_key, subject, content in rows:
        log(f"--- {msg_key}  {subject or ''}")
        for line in (content or "").split("\n")[:12]:
            log("    " + textwrap.shorten(line, 100) if line.strip() else "")
        log("")


def cmd_unresolved(conn, limit=20, log=print):
    """Messages whose sender or recipients Outlook would not resolve."""
    rows = conn.execute(
        "SELECT msg_key, sent_time, sender_name, sender_addr, recipients_dropped "
        "FROM messages WHERE sender_addr IS NULL OR recipients_dropped > 0 "
        "ORDER BY sent_time LIMIT ?", (limit,)).fetchall()
    if not rows:
        log("every address resolved")
        return
    log(f"  {'sent':<12} {'sender name':<24} {'sender addr':<26} dropped")
    log("  " + "-" * 74)
    for msg_key, sent, name, addr, dropped in rows:
        log(f"  {(sent or '')[:10]:<12} {(name or '')[:24]:<24} "
            f"{(addr or '(none)')[:26]:<26} {dropped or 0}")
    log("\n  sender names are still recorded, so you can identify who these are")


def cmd_folders(conn, rebuild=False, stores=None, log=print):
    """Show the tree. --rebuild re-reads it from Outlook; without it this is a
    pure database read and never touches Outlook."""
    if rebuild:
        source = _outlook(stores)
        n = folders.load_tree(conn, source,
                              progress=lambda done, total, path: None)
        log(f"read {n} folder(s) from Outlook")

    def walk(parent, depth):
        for f in folders.children_of(conn, parent):
            tick = "[x]" if f["selected"] else "[ ]"
            more = " ..." if f["has_children"] and not f["children_loaded"] else ""
            when = f["last_refreshed_at"] or "never"
            log(f"  {'  ' * depth}{tick} {f['name']:<28} {when}{more}")
            walk(f["path"], depth + 1)

    walk(None, 0)
    sel = folders.selected(conn)
    log(f"\n  {len(sel)} folder(s) ticked for refresh")
    if sel:
        log(f"  earliest refresh among them: "
            f"{folders.earliest_refresh(conn) or 'never (a full scan is needed)'}")


def cmd_refresh(conn, args, log=print):
    """The three refresh buttons, which differ only in where scanning starts."""
    paths = [f["path"] for f in folders.selected(conn)]
    if not paths:
        raise SystemExit("no folders ticked -- use `select` first")

    if args.full:
        since = None
    elif args.since_last:
        since = refresh.since_from_watermarks(conn, paths)
    else:
        since = refresh.since_from_cutoff(args.days, args.hours, args.minutes)
        if since is None:
            raise SystemExit(
                "give a cutoff (--days/--hours/--minutes), or --since-last, "
                "or --full")

    source = _outlook(args.stores)
    result = refresh.refresh(
        conn, source, paths=paths, since=since, process=not args.no_process,
        limit=args.limit,
        progress=lambda done, total, label: log(f"  [{done}/{total}] {label}"))

    log("\n" + result.summary())
    for path, stats in result.folders.items():
        log(f"  {path:<40} seen {stats['seen']:>5}  new {stats['stored']:>5}  "
            f"moved {stats['moved']:>3}"
            + (f"  ERROR {stats['error']}" if stats["error"] else ""))


def _outlook(stores=None):
    import outlook_com
    allowed = [s.strip() for s in stores.split(",")] if stores else None
    return outlook_com.ComSource(allowed_stores=allowed)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("command", choices=[
        "init", "folders", "select", "deselect", "refresh", "demo",
        "split", "clean", "run", "report", "runs",
        "test-patterns", "unsplit", "list-unresolved"])
    ap.add_argument("path", nargs="?", help="select/deselect: the folder path")
    ap.add_argument("--rebuild", action="store_true", help="redo every row")
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="fetch: max messages checked, including existing ones; "
                         "unsplit/list-unresolved: samples to show")
    ap.add_argument("--recursive", action="store_true",
                    help="select/deselect: include every folder underneath")
    ap.add_argument("--days", type=int, default=0, help="refresh: cutoff")
    ap.add_argument("--hours", type=int, default=0, help="refresh: cutoff")
    ap.add_argument("--minutes", type=int, default=0, help="refresh: cutoff")
    ap.add_argument("--since-last", action="store_true",
                    help="refresh: resume each folder from its own watermark")
    ap.add_argument("--full", action="store_true",
                    help="refresh: everything, ignoring cutoffs")
    ap.add_argument("--no-process", action="store_true",
                    help="refresh: fetch only, skip splitting and cleaning")
    ap.add_argument("--stores", help="comma-separated top-level mailboxes to "
                                     "walk; omit for all")
    args = ap.parse_args()
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")

    if args.command == "init":
        dbmod.init(args.db)
        return

    conn = dbmod.connect(args.db)
    try:
        if args.command == "folders":
            cmd_folders(conn, rebuild=args.rebuild, stores=args.stores)
        elif args.command in ("select", "deselect"):
            if not args.path:
                ap.error(f"{args.command} needs a folder path")
            n = folders.set_selected(conn, [args.path],
                                     selected=args.command == "select",
                                     recursive=args.recursive)
            print(f"{args.command}ed {n} folder(s)")
        elif args.command == "refresh":
            cmd_refresh(conn, args)
        elif args.command == "runs":
            for r in refresh.recent_runs(conn, args.limit or 10):
                mark = {1: "ok  ", 0: "FAIL", None: "... "}[r["ok"]]
                print(f"  {mark} {r['started_at']}  {r['command']:<8} {r['summary'] or ''}")
        elif args.command == "demo":
            import demo
            demo.load(conn)
        elif args.command == "split":
            splitter.split_messages(conn, args.rebuild, args.dry_run)
        elif args.command == "clean":
            cleaner.clean_messages(conn, args.rebuild, args.dry_run)
        elif args.command == "run":
            splitter.split_messages(conn, args.rebuild, args.dry_run)
            cleaner.clean_messages(conn, args.rebuild, args.dry_run)
        elif args.command == "report":
            cmd_report(conn)
        elif args.command == "test-patterns":
            raise SystemExit(1 if splitter.test_patterns(conn) else 0)
        elif args.command == "unsplit":
            cmd_unsplit(conn, args.limit if args.limit is not None else 5)
        elif args.command == "list-unresolved":
            cmd_unresolved(conn, args.limit if args.limit is not None else 20)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
