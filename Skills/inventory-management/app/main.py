"""DecoAI Inventory Manager — FastAPI service.

Central inventory API on the X Elite PC. Other modules (Cost Estimator,
Arduino vision sync, AI Box) read and write stock through these endpoints.
"""
import re
import sys
from pathlib import Path

# Make the shared database/ package importable regardless of launch dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from fastapi import FastAPI, HTTPException, UploadFile, File

from database.db import init_db, get_conn
from database.matching import claim, find_stock_rows, normalize_color
from database.models import Item, ItemCreate, ItemUpdate
from .extractor import extract_invoice, InvoiceLine
from .vision import detect_items, DetectedItem
from .refresh import (
    BinCount, RefreshResult, apply_counts, refresh_from_arduino,
)

app = FastAPI(title="DecoAI Inventory Manager", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/items", response_model=list[Item])
def list_items() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY item_name").fetchall()
        return [dict(r) for r in rows]


@app.get("/items/{item_id}", response_model=Item)
def get_item(item_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"item {item_id} not found")
    return dict(row)


@app.post("/items", response_model=Item, status_code=201)
def create_item(item: ItemCreate) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO items
               (item_name, color, cost_ea, rent_ea, quantity, last_purchased, bin_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item.item_name, item.color, item.cost_ea, item.rent_ea,
             item.quantity, item.last_purchased, item.bin_id),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM items WHERE id = ?", (new_id,)).fetchone()
    return dict(row)


@app.patch("/items/{item_id}", response_model=Item)
def update_item(item_id: int, patch: ItemUpdate) -> dict:
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    assignments += ", updated_at = datetime('now')"
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE items SET {assignments} WHERE id = ?",
            (*fields.values(), item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"item {item_id} not found")
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row)


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int) -> None:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"item {item_id} not found")


def _upsert_line(conn, line: InvoiceLine, invoice_date: str | None) -> dict:
    """Merge one invoice line into inventory.

    Matches an existing item by name+color (case-insensitive). If found, adds to
    quantity and refreshes cost_ea / last_purchased. Otherwise inserts a new item.
    line.rent_ea is None when the caller hasn't decided a rental price yet (e.g. raw
    extraction, before reusability/rent has been judged) — only a non-None value is
    ever written, so an existing item's rent_ea is never clobbered back to 0.
    Returns {item_id, item_name, action, quantity}.
    """
    row = conn.execute(
        """SELECT id, quantity FROM items
           WHERE lower(item_name) = lower(?)
             AND ifnull(lower(color), '') = ifnull(lower(?), '')""",
        (line.item_name, line.color),
    ).fetchone()

    if row is None:
        if line.rent_ea is not None:
            cur = conn.execute(
                """INSERT INTO items (item_name, color, cost_ea, rent_ea, quantity, last_purchased)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (line.item_name, line.color, line.cost_ea, line.rent_ea, line.quantity, invoice_date),
            )
        else:
            cur = conn.execute(
                """INSERT INTO items (item_name, color, cost_ea, quantity, last_purchased)
                   VALUES (?, ?, ?, ?, ?)""",
                (line.item_name, line.color, line.cost_ea, line.quantity, invoice_date),
            )
        return {"item_id": cur.lastrowid, "item_name": line.item_name,
                "action": "created", "quantity": line.quantity}

    new_qty = row["quantity"] + line.quantity
    if line.rent_ea is not None:
        conn.execute(
            """UPDATE items
               SET quantity = ?, cost_ea = ?, rent_ea = ?, last_purchased = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (new_qty, line.cost_ea, line.rent_ea, invoice_date, row["id"]),
        )
    else:
        conn.execute(
            """UPDATE items
               SET quantity = ?, cost_ea = ?, last_purchased = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (new_qty, line.cost_ea, invoice_date, row["id"]),
        )
    return {"item_id": row["id"], "item_name": line.item_name,
            "action": "restocked", "quantity": new_qty}


@app.post("/invoices/upload")
async def upload_invoice(file: UploadFile = File(...)) -> dict:
    """Owner uploads a purchase invoice; a model reads it and inventory is updated.

    Accepts an image or PDF, extracts line items via the configured model backend
    (AI Box LLM if INVOICE_READ_MODEL_URL is set, else a mock), and upserts each line
    into the shared inventory DB.
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "empty file")

    extraction = extract_invoice(content, file.filename or "invoice")
    if not extraction.lines:
        raise HTTPException(422, "no line items could be extracted from the invoice")

    results = []
    with get_conn() as conn:
        for line in extraction.lines:
            results.append(_upsert_line(conn, line, extraction.invoice_date))

    return {
        "filename": file.filename,
        "invoice_date": extraction.invoice_date,
        "lines_extracted": len(extraction.lines),
        "results": results,
    }


def _check_against_inventory(conn, item: DetectedItem, consumed: dict = None) -> dict:
    """Compare one detected item to inventory stock.

    Three tiers, most precise first: name+color, then name-only (a photo's
    perceived color often differs from the label), then approximate name match
    for the generic names a vision model produces.

    Quantities are summed across every matching row rather than taken from the
    first — the same product can occupy several rows (one per color), so
    reading one row understated stock.

    Returns the item plus in_stock / shortfall, a present|partial|missing
    status, and matched_item_name naming what it actually matched.

    `consumed` carries stock already claimed by earlier detections in the same
    analysis. Several detected rows routinely resolve to one inventory row (a
    photo yields "Balloon" in four colors, inventory holds one "Golden Star
    Balloons"); without this, each row is measured against the full quantity
    and 45 detected balloons all read "present" against 16 in stock — telling
    the owner to buy nothing when 29 are short. Pass a dict to allocate the
    stock across them instead; omit it to check one item in isolation.
    """
    color = normalize_color(item.color)
    rows = find_stock_rows(conn, item.item_name, color,
                           columns="id, item_name, quantity, bin_id")

    stock = sum(r["quantity"] for r in rows)
    matched = sorted({r["item_name"] for r in rows})

    stock = claim(consumed, matched, stock, item.quantity)
    shortfall = max(0, item.quantity - stock)
    if stock == 0:
        status = "missing"          # not on the shelf at all
    elif shortfall > 0:
        status = "partial"          # some in stock, not enough
    else:
        status = "present"          # fully covered

    return {
        "item_name": item.item_name,
        "color": color,
        "detected_quantity": item.quantity,
        "in_stock": stock,
        "shortfall": shortfall,
        "status": status,
        "matched_item_name": ", ".join(matched) or None,
        "bin_id": rows[0]["bin_id"] if rows else None,
    }


@app.post("/images/analyze")
async def analyze_image(file: UploadFile = File(...)) -> dict:
    """Owner uploads a decoration photo; detected items are checked against stock.

    A vision model lists the decoration items in the image (AI Box Qwen-VL if
    IMAGE_READ_MODEL_URL is set, else a mock), then each item is compared to the shared
    inventory DB. Returns a per-item breakdown plus a `missing_items` list ready
    to feed the Amazon URL Builder.
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "empty file")

    detection = detect_items(content, file.filename or "image")
    if not detection.items:
        raise HTTPException(422, "no decoration items could be detected in the image")

    results = []
    consumed = {}   # stock claimed so far, shared across all detected items
    with get_conn() as conn:
        for item in detection.items:
            results.append(_check_against_inventory(conn, item, consumed))

    return {
        "filename": file.filename,
        "items_detected": len(detection.items),
        "present": [r for r in results if r["status"] == "present"],
        "partial": [r for r in results if r["status"] == "partial"],
        "missing": [r for r in results if r["status"] == "missing"],
        "results": results,
        # Ready to POST straight to the Amazon URL Builder
        "missing_items": [
            {"item_name": r["item_name"], "color": r["color"], "quantity": r["shortfall"]}
            for r in results if r["shortfall"] > 0
        ],
    }


@app.post("/refresh", response_model=RefreshResult)
def refresh_now() -> RefreshResult:
    """Step 3 — trigger a shelf refresh: get counts, reconcile the DB, list reorders.

    Polls the Arduino when ARDUINO_URL is set; otherwise generates dummy counts
    for the bins already in the DB so the flow works without hardware. The
    response's `source` field says which was used ("arduino" or "dummy").
    """
    try:
        return refresh_from_arduino()
    except Exception as e:
        raise HTTPException(502, f"refresh failed: {e}")


@app.post("/refresh/bins", response_model=RefreshResult)
def refresh_with_counts(counts: list[BinCount]) -> RefreshResult:
    """Reconcile the DB from an explicit list of bin counts.

    Lets the Arduino push counts directly, and lets you test the reconcile +
    reorder logic without a live Arduino. Vision count is authoritative (set).
    """
    return apply_counts(counts)
