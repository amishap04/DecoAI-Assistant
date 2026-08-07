#!/usr/bin/env python3
"""Step 3 CLI — refresh shelf stock from the Arduino (or dummy data).

Gets per-bin item counts, sets each bin's quantity in the shared inventory DB to
the counted value (vision = truth), then reports items at/below the reorder
threshold. Uses the real Arduino when ARDUINO_URL is set; otherwise generates
dummy counts so the flow works without hardware. Talks to the DB directly — no
server required.

Counts come from the Arduino Uno Q's VLM when it is reachable (the `unoq` skill
counts the shelf over the ESP32-CAM), else the HTTP service at ARDUINO_URL, else
dummy data. Every source is written to the same shared DB.

Usage:
    decoai-refresh [--json] [--dry-run] [--unoq] [--dummy] [--reorder-only]
    decoai-refresh --commit-file <ask_vlm-reply.txt|->

    --dry-run       show the counts that would be applied; don't touch the DB
    --unoq          force the Uno Q VLM counts; fail instead of falling back
    --dummy         force dummy counts even if the Uno Q or ARDUINO_URL is available
    --reorder-only  emit just the reorder JSON array (for the Amazon URL Builder)
    --commit-file   commit a reply you already got from the unoq skill's
                    ask_vlm.py (file path, or '-' to read stdin); no capture

Exit codes: 0 = success, 1 = Uno Q / Arduino unreachable.
"""
import argparse
import json
import os
import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parents[1]      # inventory-management/ (has app/)
sys.path.insert(0, str(_SVC.parent))            # repo root (has database/)
sys.path.insert(0, str(_SVC))

from dotenv import load_dotenv                                         # noqa: E402
load_dotenv(_SVC.parent / ".env")

from database.db import init_db                                        # noqa: E402
from app.refresh import (apply_counts, apply_item_counts, dummy_counts,   # noqa: E402
                         has_bins, parse_vlm_items, poll_arduino, poll_unoq,
                         poll_unoq_items, unoq_script)


def describe(c, item_mode: bool) -> str:
    """One count line, per item or per bin depending on the mode."""
    if item_mode:
        return "  " + " ".join(x for x in (c.color, c.item_name) if x) + f": {c.count}"
    return f"  bin {c.bin_id}: {c.count}"


def report(result, counts, source: str, item_mode: bool, args) -> int:
    """Print the outcome in whichever shape the caller asked for."""
    reorder = [r.model_dump() for r in result.reorder_items]

    if args.reorder_only:
        print(json.dumps(reorder))
        return 0

    if args.json:
        print(json.dumps(result.model_dump(), indent=2))
        return 0

    unit = "item" if item_mode else "bin"
    print(f"Source: {source}  -  {result.bins_seen} {unit}(s) reported")
    for c in counts:
        print(describe(c, item_mode))
    print(f"Updated {result.items_updated} item(s) in the DB"
          + (f", added {result.items_created} new item(s)." if result.items_created else "."))
    if reorder:
        print(f"\n{len(reorder)} item(s) need reordering:")
        for r in reorder:
            name = " ".join(x for x in (r.get("color"), r["item_name"]) if x)
            print(f"  - {name} (qty {r['quantity']}, bin {r.get('bin_id')})")
    else:
        print("\nNothing to reorder.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-refresh",
        description="Step 3: refresh shelf stock from the Arduino vision counts.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--dry-run", action="store_true",
                        help="show counts without updating the DB")
    parser.add_argument("--unoq", action="store_true",
                        help="force Uno Q VLM counts; fail instead of falling back")
    parser.add_argument("--latest", action="store_true",
                        help="count the frame already on the board (the one you just "
                             "reviewed) instead of capturing a new one")
    parser.add_argument("--dummy", action="store_true",
                        help="force dummy counts even if the Uno Q or ARDUINO_URL is available")
    parser.add_argument("--reorder-only", action="store_true",
                        help="emit only the reorder items JSON array")
    parser.add_argument("--commit-file", metavar="PATH",
                        help="commit an ask_vlm.py reply saved to PATH ('-' for stdin); "
                             "no capture is performed")
    args = parser.parse_args()

    init_db()

    # Commit an answer the owner/agent already got from ask_vlm.py.
    if args.commit_file:
        try:
            raw = (sys.stdin.read() if args.commit_file == "-"
                   else Path(args.commit_file).read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            print(f"ERROR: cannot read {args.commit_file}: {e}", file=sys.stderr)
            return 1
        try:
            counts = parse_vlm_items(raw)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if not counts:
            print(f"ERROR: no item counts found in {args.commit_file}", file=sys.stderr)
            return 2
        return report(apply_item_counts(counts, source="unoq"), counts,
                      "unoq", True, args)

    counts, source, item_mode = None, "dummy", False

    # Uno Q first: forced with --unoq (errors are fatal), otherwise tried
    # opportunistically when the unoq skill is installed. With no labelled bins
    # on the shelf, the board counts items instead and matching happens by name.
    if not args.dummy and (args.unoq or unoq_script().is_file()):
        item_mode = not has_bins()
        try:
            if item_mode:
                counts = poll_unoq_items(use_latest=args.latest)
            else:
                counts = poll_unoq(use_latest=args.latest)
            source = "unoq"
        except Exception as e:
            item_mode = False
            if args.unoq:
                print(f"ERROR: Uno Q unreachable: {e}", file=sys.stderr)
                return 1
            print(f"WARNING: Uno Q unavailable ({e}); falling back", file=sys.stderr)

    if counts is None and not args.dummy and os.environ.get("ARDUINO_URL"):
        try:
            counts = poll_arduino()
            source = "arduino"
        except Exception as e:
            print(f"ERROR: Arduino unreachable: {e}", file=sys.stderr)
            return 1

    if counts is None:
        counts = dummy_counts()
        source = "dummy"

    if args.dry_run:
        payload = {"source": source, "dry_run": True, "mode": "items" if item_mode else "bins",
                   "counts": [c.model_dump() for c in counts]}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            unit = "item" if item_mode else "bin"
            print(f"[dry-run] {source} counts for {len(counts)} {unit}(s):")
            for c in counts:
                print(describe(c, item_mode))
            print("[dry-run] DB not modified")
        return 0

    if item_mode:
        result = apply_item_counts(counts, source=source)
    else:
        result = apply_counts(counts, source=source)
    return report(result, counts, source, item_mode, args)


if __name__ == "__main__":
    raise SystemExit(main())
