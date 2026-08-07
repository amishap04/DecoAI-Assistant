#!/usr/bin/env python3
"""Step 2 CLI — analyze a decoration image and check items against inventory.

Detects decoration items in a photo with the configured vision backend
(IMAGE_READ_MODEL_URL if set, else a mock), then cross-checks each against the shared
inventory DB: present (enough stock), partial (some, not enough), or missing.
Talks to the DB directly — no server required.

Usage:
    decoai-image <image-file> [--json] [--missing-only]

--missing-only prints just the missing_items JSON array, ready to pipe into the
Amazon URL Builder.

Exit codes: 0 = success, 1 = bad input / file error, 2 = nothing detected.
"""
import argparse
import json
import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parents[1]      # inventory-management/ (has app/)
sys.path.insert(0, str(_SVC.parent))            # repo root (has database/)
sys.path.insert(0, str(_SVC))

from dotenv import load_dotenv                                         # noqa: E402
load_dotenv(_SVC.parent / ".env")

from database.db import init_db, get_conn              # noqa: E402
from app.vision import detect_items                   # noqa: E402
from app.main import _check_against_inventory         # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-image",
        description="Step 2: detect items in a decoration image and check inventory.",
    )
    parser.add_argument("image", help="path to the decoration photo")
    parser.add_argument("--json", action="store_true", help="emit full JSON instead of text")
    parser.add_argument("--missing-only", action="store_true",
                        help="emit only the missing_items JSON array")
    args = parser.parse_args()

    path = Path(args.image)
    if not path.is_file():
        print(f"ERROR: no such file: {path}", file=sys.stderr)
        return 1
    content = path.read_bytes()
    if not content:
        print("ERROR: file is empty", file=sys.stderr)
        return 1

    init_db()
    detection = detect_items(content, path.name)
    if not detection.items:
        print("ERROR: no decoration items detected in the image", file=sys.stderr)
        return 2

    results = []
    consumed = {}   # stock claimed so far, shared across all detected items
    with get_conn() as conn:
        for item in detection.items:
            results.append(_check_against_inventory(conn, item, consumed))

    missing_items = [
        {"item_name": r["item_name"], "color": r["color"], "quantity": r["shortfall"]}
        for r in results if r["shortfall"] > 0
    ]

    if args.missing_only:
        print(json.dumps(missing_items))
        return 0

    payload = {
        "filename": path.name,
        "items_detected": len(detection.items),
        "present": [r for r in results if r["status"] == "present"],
        "partial": [r for r in results if r["status"] == "partial"],
        "missing": [r for r in results if r["status"] == "missing"],
        "results": results,
        "missing_items": missing_items,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Image: {path.name}  -  {len(detection.items)} item(s) detected\n")
        print(f"{'ITEM':<18}{'DETECTED':>9}{'IN STOCK':>9}{'SHORT':>7}  STATUS")
        for r in results:
            # Name the stocked item whenever it isn't the detected name verbatim,
            # so an approximate match is never silently taken on trust.
            matched = r.get("matched_item_name")
            note = (f"   [matched: {matched}]"
                    if matched and matched.lower() != r["item_name"].lower() else "")
            print(f"{r['item_name']:<18}{r['detected_quantity']:>9}"
                  f"{r['in_stock']:>9}{r['shortfall']:>7}  {r['status']}{note}")
        if missing_items:
            print(f"\nNeed to buy {len(missing_items)} item(s):")
            for m in missing_items:
                name = " ".join(x for x in (m["color"], m["item_name"]) if x)
                print(f"  - {name} x{m['quantity']}")
        else:
            print("\nEverything needed is in stock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
