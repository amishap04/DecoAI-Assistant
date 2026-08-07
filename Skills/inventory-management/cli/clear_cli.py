#!/usr/bin/env python3
"""Maintenance CLI — clear the shared inventory DB (DELETE FROM items).

Wipes every row from the items table, leaving the schema intact so the next
invoice upload or shelf refresh starts from an empty inventory. A timestamped
copy of the DB file is written first unless --no-backup is passed.

This is destructive and cannot be undone except from that backup, so it refuses
to run without the explicit --yes flag.

Usage:
    decoai-clear --yes [--json] [--no-backup]

    --yes         required; confirms the wipe
    --no-backup   skip the .bak copy taken before deleting
    --json        emit JSON instead of a text summary

Exit codes: 0 = cleared, 1 = backup failed, 2 = --yes not given.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_SVC = Path(__file__).resolve().parents[1]      # inventory-management/ (has app/)
sys.path.insert(0, str(_SVC.parent))            # repo root (has database/)
sys.path.insert(0, str(_SVC))

from database.db import DB_PATH, get_conn, init_db                    # noqa: E402


def backup_db() -> Path:
    """Copy the DB file next to itself with a timestamp; returns the new path."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DB_PATH.with_name(f"{DB_PATH.name}.bak-clear-{stamp}")
    shutil.copy2(DB_PATH, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-clear",
        description="Delete every row from the shared inventory DB (items table).",
    )
    parser.add_argument("--yes", action="store_true",
                        help="confirm the wipe (required)")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the backup copy taken before deleting")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of text")
    args = parser.parse_args()

    init_db()

    with get_conn() as conn:
        before = conn.execute("SELECT count(*) FROM items").fetchone()[0]

    if not args.yes:
        print(f"Refusing to clear {before} item(s) without --yes.", file=sys.stderr)
        print(f"  DB: {DB_PATH}", file=sys.stderr)
        print("  Re-run with: clear_cli.py --yes", file=sys.stderr)
        return 2

    backup = None
    if not args.no_backup and DB_PATH.is_file():
        try:
            backup = backup_db()
        except OSError as e:
            print(f"ERROR: backup failed, nothing deleted: {e}", file=sys.stderr)
            return 1

    with get_conn() as conn:
        conn.execute("DELETE FROM items")

    with get_conn() as conn:
        after = conn.execute("SELECT count(*) FROM items").fetchone()[0]

    if args.json:
        print(json.dumps({"deleted": before - after, "remaining": after,
                          "backup": str(backup) if backup else None,
                          "db": str(DB_PATH)}, indent=2))
    else:
        print(f"Cleared {before - after} item(s); {after} remaining.")
        print(f"  DB: {DB_PATH}")
        if backup:
            print(f"  Backup: {backup}")
        else:
            print("  Backup: skipped (--no-backup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
