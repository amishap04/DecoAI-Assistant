#!/usr/bin/env python3
"""Print the current contents of the shared inventory DB as a formatted table.

Usage:
    python database/db_dump.py
    python database/db_dump.py --json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_conn  # noqa: E402

_COLUMNS = ["id", "item_name", "color", "cost_ea", "rent_ea", "quantity",
            "last_purchased", "bin_id", "updated_at"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump the DecoAI inventory DB.")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY item_name")]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print("(no items in the database yet)")
        return 0

    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in _COLUMNS}
    header = "  ".join(c.ljust(widths[c]) for c in _COLUMNS)
    print(header)
    print("  ".join("-" * widths[c] for c in _COLUMNS))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in _COLUMNS))
    print(f"\n{len(rows)} item(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
