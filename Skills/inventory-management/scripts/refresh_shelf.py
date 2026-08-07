#!/usr/bin/env python3
"""Shelf refresh script — run on a schedule by OpenClaw (the CRON JOB in Diagram 2).

Flow each run:
    1. Contact the Arduino Uno Q (YOLOv8 vision) for current per-bin shelf counts.
    2. Push those counts to the Inventory Manager, which sets each bin's quantity
       to the vision count (vision = truth) and returns items to reorder.
    3. Print a summary (and the reorder list) so OpenClaw can act on it — e.g.
       feed the missing items to the Amazon URL Builder.

This is a PLACEHOLDER: the Arduino call is stubbed (see `get_shelf_counts`) until
the Uno Q's vision endpoint / serial protocol is finalized. Everything else works.

Usage:
    python scripts/refresh_shelf.py
    python scripts/refresh_shelf.py --dry-run      # poll Arduino, don't write to DB

Config (env vars):
    ARDUINO_URL     base URL of the Arduino vision service (e.g. http://uno-q.local:8080)
    INVENTORY_URL   Inventory Manager base URL (default http://localhost:8005)

Exit codes: 0 = success, 1 = Arduino unreachable, 2 = Inventory Manager error.
"""
import argparse
import os
import sys

import httpx

ARDUINO_URL = os.environ.get("ARDUINO_URL", "http://uno-q.local:8080")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://localhost:8005")


def get_shelf_counts() -> list[dict]:
    """Contact the Arduino Uno Q and return per-bin YOLO counts.

    Expected shape: [{"bin_id": "A1", "count": 42}, ...]

    TODO(arduino): replace the stub below with the real Uno Q integration once the
    vision endpoint is finalized. Two likely options:
      - HTTP:   GET {ARDUINO_URL}/bins  ->  the JSON above
      - Serial: read a line-delimited JSON frame over USB/UART (pyserial)
    The HTTP path is scaffolded and commented out; the stub returns sample data so
    the rest of the pipeline is runnable today.
    """
    # --- Real HTTP implementation (uncomment once the Uno Q endpoint is live) ---
    # resp = httpx.get(f"{ARDUINO_URL.rstrip('/')}/bins", timeout=15)
    # resp.raise_for_status()
    # return resp.json()

    # --- Placeholder sample data ---
    print(f"[placeholder] would poll Arduino at {ARDUINO_URL}/bins", file=sys.stderr)
    return [
        {"bin_id": "A1", "count": 120},
        {"bin_id": "B3", "count": 6},
    ]


def push_to_inventory(counts: list[dict]) -> dict:
    """Send bin counts to the Inventory Manager's /refresh/bins endpoint."""
    resp = httpx.post(
        f"{INVENTORY_URL.rstrip('/')}/refresh/bins",
        json=counts,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh shelf inventory from the Arduino.")
    parser.add_argument("--dry-run", action="store_true",
                        help="poll the Arduino and print counts, but don't update the DB")
    args = parser.parse_args()

    try:
        counts = get_shelf_counts()
    except Exception as e:
        print(f"ERROR: could not reach Arduino: {e}", file=sys.stderr)
        return 1

    print(f"Arduino reported {len(counts)} bin(s): {counts}")

    if args.dry_run:
        print("[dry-run] skipping inventory update")
        return 0

    try:
        result = push_to_inventory(counts)
    except Exception as e:
        print(f"ERROR: Inventory Manager update failed: {e}", file=sys.stderr)
        return 2

    print(f"Updated {result['items_updated']} item(s) across {result['bins_seen']} bin(s).")
    reorder = result.get("reorder_items", [])
    if reorder:
        print(f"{len(reorder)} item(s) need reordering:")
        for item in reorder:
            name = " ".join(x for x in (item.get("color"), item["item_name"]) if x)
            print(f"  - {name} (qty {item['quantity']}, bin {item.get('bin_id')})")
    else:
        print("Nothing to reorder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
