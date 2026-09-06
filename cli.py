"""One entry point for the whole pipeline.

    python3 cli.py mail.db init            create tables, seed default patterns
    python3 cli.py mail.db fetch           read mail from classic Outlook
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


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("command", choices=[
        "init", "fetch", "demo", "split", "clean", "run", "report",
        "test-patterns", "unsplit", "list-unresolved"])
    ap.add_argument("--rebuild", action="store_true", help="redo every row")
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="fetch: max messages checked, including existing ones; "
                         "unsplit/list-unresolved: samples to show")
    ap.add_argument("--folder", help="fetch: folder path, e.g. 'Inbox/Clients'")
    ap.add_argument("--recurse", action="store_true",
                    help="fetch: include subfolders")
    ap.add_argument("--since-days", type=int, default=30,
                    help="fetch: how far back to look (0 for everything)")
    ap.add_argument("--no-restrict", action="store_true",
                    help="fetch: filter in Python instead of asking Outlook "
                         "(slower, but immune to date-format trouble)")
    args = ap.parse_args()
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")

    if args.command == "init":
        dbmod.init(args.db)
        return

    conn = dbmod.connect(args.db)
    try:
        if args.command == "fetch":
            import fetch
            fetch.fetch_messages(
                conn, folder=args.folder, recurse=args.recurse,
                since_days=args.since_days, use_restrict=not args.no_restrict,
                limit=args.limit)
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
