#!/usr/bin/env python3
"""Cost Estimator CLI — itemized have-vs-need cost estimate for a decoration concept.

Takes the items a decoration concept needs, checks the shared inventory DB, and
reports what's already on the shelf vs. what must be bought. Pricing is DB-only:
items missing a known cost come back with cost_ea=0 and price_source="missing" —
pricing those is left to the caller (e.g. OpenClaw's own LLM), fed by
missing_items. Talks to the DB directly — no server required.

Usage:
    decoai-estimate <items-json> [--json]
    decoai-estimate --file <items.json> [--json]

<items-json> is a JSON array of {"item_name": str, "color"?: str, "quantity"?: int}.

Exit codes: 0 = success, 1 = bad input.
"""
import argparse
import json
import sys
from pathlib import Path

_SVC = Path(__file__).resolve().parents[1]      # cost-estimation/ (has app/)
sys.path.insert(0, str(_SVC.parent))            # repo root (has database/)
sys.path.insert(0, str(_SVC))

from dotenv import load_dotenv                                         # noqa: E402
load_dotenv(_SVC.parent / ".env")

from database.db import init_db                     # noqa: E402
from app.estimator import estimate, NeededItem      # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-estimate",
        description="Itemized cost estimate for a decoration concept's needed items.",
    )
    parser.add_argument("items", nargs="?",
                        help="JSON array of {item_name, color?, quantity?}")
    parser.add_argument("--file", help="path to a JSON file with the items array instead")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    if args.file:
        raw = Path(args.file).read_text()
    elif args.items:
        raw = args.items
    else:
        print("ERROR: provide items JSON as an argument or via --file", file=sys.stderr)
        return 1

    try:
        payload = json.loads(raw)
        needed = [NeededItem(**item) for item in payload]
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"ERROR: invalid items JSON: {e}", file=sys.stderr)
        return 1

    init_db()
    result = estimate(needed)

    if args.json:
        print(result.model_dump_json())
    else:
        print(f"{'ITEM':<18}{'NEEDED':>7}{'IN STOCK':>9}{'MISSING':>8}{'COST EA':>9}{'LINE COST':>10}  SOURCE")
        for line in result.lines:
            name = " ".join(x for x in (line.color, line.item_name) if x)
            print(f"{name:<18}{line.needed:>7}{line.in_stock:>9}{line.missing:>8}"
                  f"{line.cost_ea:>9.2f}{line.line_cost:>10.2f}  {line.price_source}")
        print(f"\nTotal (DB-priced only): ${result.total_cost:.2f}")
        if result.missing_items:
            print(f"\n{len(result.missing_items)} item(s) need pricing/purchase:")
            for m in result.missing_items:
                name = " ".join(x for x in (m.color, m.item_name) if x)
                print(f"  - {name} x{m.quantity}")
        else:
            print("\nEverything needed is in stock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
