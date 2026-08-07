#!/usr/bin/env python3
"""Step 1 CLI — upload an invoice and add its items to the inventory DB.

Reads an invoice image/PDF, extracts line items with the configured model
backend (INVOICE_READ_MODEL_URL if set, else a mock), and upserts each line into the
shared inventory DB. Talks to the DB directly — no server required.

Three modes:

    decoai-invoice <invoice-file> [--json]
        One-shot: extract AND commit immediately. rent_ea is never set by
        extraction, so this never overwrites an existing item's rent price
        (see --commit-file for the agent-reasoned flow).

    decoai-invoice <invoice-file> --extract-only
        Extract only — prints the raw line items (rent_ea always null) as
        JSON. No DB writes. Meant for an agent to review each item, decide
        reusability, and fill in rent_ea per line before committing.

    decoai-invoice --commit-file <path> [--json]
        Reads a JSON file matching the extraction shape (invoice_date + lines,
        each optionally carrying a decided rent_ea) and upserts every line.
        Use after --extract-only once rent_ea decisions have been made.

Exit codes: 0 = success, 1 = bad input / file error, 2 = no lines extracted.
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
from app.extractor import extract_invoice, InvoiceExtraction           # noqa: E402
from app.main import _upsert_line                       # noqa: E402


def _commit(extraction: InvoiceExtraction) -> dict:
    """Upsert every line of an extraction into the DB. Shared by all commit paths."""
    init_db()
    results = []
    with get_conn() as conn:
        for line in extraction.lines:
            results.append(_upsert_line(conn, line, extraction.invoice_date))
    return {
        "invoice_date": extraction.invoice_date,
        "lines_extracted": len(extraction.lines),
        "results": results,
    }


def _print_commit_result(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Invoice date: {payload['invoice_date']}")
        print(f"Extracted {payload['lines_extracted']} line item(s):")
        for r in payload["results"]:
            print(f"  [{r['action']:<9}] {r['item_name']:<20} qty now {r['quantity']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-invoice",
        description="Extract items from an invoice and add them to inventory.",
    )
    parser.add_argument("invoice", nargs="?", help="path to the invoice image or PDF")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--extract-only", action="store_true",
                        help="extract line items and print JSON only; no DB writes")
    parser.add_argument("--commit-file", metavar="PATH",
                        help="commit a previously-extracted (and rent-reasoned) JSON file")
    args = parser.parse_args()

    if args.commit_file:
        path = Path(args.commit_file)
        if not path.is_file():
            print(f"ERROR: no such file: {path}", file=sys.stderr)
            return 1
        try:
            extraction = InvoiceExtraction.model_validate(json.loads(path.read_text()))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"ERROR: invalid commit file: {e}", file=sys.stderr)
            return 1
        if not extraction.lines:
            print("ERROR: commit file has no line items", file=sys.stderr)
            return 2
        _print_commit_result(_commit(extraction), args.json)
        return 0

    if not args.invoice:
        print("ERROR: an invoice file is required unless --commit-file is used", file=sys.stderr)
        return 1

    path = Path(args.invoice)
    if not path.is_file():
        print(f"ERROR: no such file: {path}", file=sys.stderr)
        return 1
    content = path.read_bytes()
    if not content:
        print("ERROR: file is empty", file=sys.stderr)
        return 1

    extraction = extract_invoice(content, path.name)
    if not extraction.lines:
        print("ERROR: no line items could be extracted from the invoice", file=sys.stderr)
        return 2

    if args.extract_only:
        print(extraction.model_dump_json(indent=2))
        return 0

    payload = {"filename": path.name, **_commit(extraction)}
    _print_commit_result(payload, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
