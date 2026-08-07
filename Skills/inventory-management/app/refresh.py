"""Inventory Refresh — the 'Refresh (CRON JOB)' node from Diagram 2.

On a schedule, the PC polls the Arduino (which runs YOLOv8 vision over the
shelf) for per-bin item counts, then reconciles them into the shared inventory
DB. The vision count is treated as truth: each bin's `quantity` is SET to what
the Arduino sees. After syncing, a reorder check collects items at/below a
threshold into a missing-items list (same shape the Cost Estimator and Amazon
URL Builder consume).

Counts can come from three places, in this order of preference: the Arduino
Uno Q (the `unoq` skill's VLM counts the shelf over the ESP32-CAM), an HTTP
vision service at ARDUINO_URL, or generated dummy data. Whichever is used, the
counts land in the same shared DB through apply_counts().

Config (env vars):
    UNOQ_SCRIPT              path to the unoq skill's shelf_counts.py. Defaults to
                             ../UnoQ-ESP32-VLM/scripts/shelf_counts.py next to this skill.
    UNOQ_TIMEOUT_SECONDS     how long to wait on the board's VLM (default 900 —
                             vision on Qwen3.5-2B takes minutes).
    ARDUINO_URL              base URL of the Arduino vision service. If unset, a
                             DUMMY generator is used instead (see dummy_counts)
                             so the full refresh flow is testable without hardware.
    REFRESH_INTERVAL_SECONDS polling interval for external schedulers (default 300).
    REORDER_THRESHOLD        quantity at/below which an item is flagged to reorder
                             (default 5).

Arduino contract:
    GET {ARDUINO_URL}/bins -> [{"bin_id": "A1", "count": 42}, ...]
"""
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from database.db import get_conn


class BinCount(BaseModel):
    """One bin's YOLO vision count from the Arduino."""
    bin_id: str
    count: int = Field(ge=0)


class ReorderItem(BaseModel):
    """An item at/below the reorder threshold — feeds the Amazon URL builder."""
    item_name: str
    color: Optional[str] = None
    quantity: int
    bin_id: Optional[str] = None


class ItemCount(BaseModel):
    """One item's count from the Uno Q VLM, for shelves with no labelled bins."""
    item_name: str
    color: Optional[str] = None
    count: int = Field(ge=0)


class RefreshResult(BaseModel):
    source: str                 # "unoq", "arduino" or "dummy" — where counts came from
    bins_seen: int              # bins (or distinct items, in item mode) reported
    items_updated: int          # rows whose quantity changed
    items_created: int = 0      # rows added for items seen but not yet in the DB
    reorder_items: list[ReorderItem]


def _threshold() -> int:
    try:
        return int(os.environ.get("REORDER_THRESHOLD", "5"))
    except ValueError:
        return 5


def dummy_counts() -> list[BinCount]:
    """Generate plausible shelf counts for every bin currently in the DB.

    Stands in for the Arduino's YOLOv8 vision so the whole refresh path can be
    exercised without hardware. Counts are random but deliberately spread across
    the reorder threshold, so each run produces a mix of well-stocked and
    low-stock bins (i.e. a non-empty reorder list most of the time).
    """
    with get_conn() as conn:
        bins = [r["bin_id"] for r in conn.execute(
            "SELECT DISTINCT bin_id FROM items WHERE bin_id IS NOT NULL ORDER BY bin_id"
        ).fetchall()]

    threshold = _threshold()
    counts: list[BinCount] = []
    for i, bin_id in enumerate(bins):
        # Alternate: some bins land at/below the threshold, others well above it.
        if i % 2 == 0:
            counts.append(BinCount(bin_id=bin_id, count=random.randint(0, threshold)))
        else:
            counts.append(BinCount(bin_id=bin_id, count=random.randint(threshold + 10, 200)))
    return counts


def poll_arduino() -> list[BinCount]:
    """Fetch per-bin vision counts from the Arduino. Raises if unreachable."""
    base = os.environ.get("ARDUINO_URL")
    if not base:
        raise RuntimeError("ARDUINO_URL is not set")
    resp = httpx.get(f"{base.rstrip('/')}/bins", timeout=15)
    resp.raise_for_status()
    return [BinCount.model_validate(b) for b in resp.json()]


def unoq_script() -> Path:
    """Location of the unoq skill's shelf_counts.py."""
    override = os.environ.get("UNOQ_SCRIPT")
    if override:
        return Path(override)
    skills_root = Path(__file__).resolve().parents[2]        # Skills/
    return skills_root / "UnoQ-ESP32-VLM" / "scripts" / "shelf_counts.py"


def _run_unoq(mode: str, use_latest: bool = False) -> list[dict]:
    """Run the unoq skill's shelf_counts.py and return its parsed JSON.

    Shells out so all the board/adb/VLM handling lives in one place.
    """
    script = unoq_script()
    if not script.is_file():
        raise RuntimeError(f"unoq skill script not found: {script}")

    try:
        timeout = int(os.environ.get("UNOQ_TIMEOUT_SECONDS", "900"))
    except ValueError:
        timeout = 900

    cmd = [sys.executable, str(script), "--mode", mode]
    if use_latest:
        cmd.append("--latest")      # count the frame already reviewed, don't recapture

    proc = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=timeout,
        # The board's helper scripts emit UTF-8 (curly quotes, arrows); Windows
        # would otherwise decode them as cp1252 and blow up mid-read.
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"unoq shelf count failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or 'no error output'}"
        )
    # The board's capture helper prints progress on stdout too, so take the last
    # line that parses as a JSON array — shelf_counts.py emits its result last.
    data = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        break
    if data is None:
        raise RuntimeError(f"unoq returned non-JSON output: {proc.stdout!r}")
    if not data:
        raise RuntimeError("unoq reported no counts")
    return data


def parse_vlm_items(reply: str) -> list[ItemCount]:
    """Turn a raw Uno Q VLM reply into item counts.

    Lets the owner (or the agent) run `ask_vlm.py` by hand, eyeball the answer,
    and commit it afterwards — the reply is JSON wrapped in whatever prose the
    model felt like adding, so parsing goes through the unoq skill's parser
    rather than a second copy of the same rules.
    """
    script = unoq_script()
    if not script.is_file():
        raise RuntimeError(f"unoq skill script not found: {script}")

    spec = importlib.util.spec_from_file_location("unoq_shelf_counts", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # The board's capture helper writes progress to stdout as well, so a piped
    # reply is the answer buried in noise (including an ANSI "[2K" that would
    # otherwise anchor a whole-text regex). Prefer the last line that parses.
    for line in reversed(reply.splitlines()):
        if "[" not in line:
            continue
        items = module.parse_item_counts(line)
        if items:
            return [ItemCount.model_validate(i) for i in items]

    return [ItemCount.model_validate(i) for i in module.parse_item_counts(reply)]


def poll_unoq(use_latest: bool = False) -> list[BinCount]:
    """Per-bin counts from the Uno Q VLM. Same contract as the HTTP Arduino."""
    return [BinCount.model_validate(b) for b in _run_unoq("bins", use_latest)]


def poll_unoq_items(use_latest: bool = False) -> list[ItemCount]:
    """Per-item counts from the Uno Q VLM — for shelves with no labelled bins."""
    return [ItemCount.model_validate(i) for i in _run_unoq("items", use_latest)]


def apply_counts(counts: list[BinCount], source: str = "arduino") -> RefreshResult:
    """Set each bin's quantity to its vision count, then run the reorder check.

    Vision count is authoritative (set, not delta). Only rows whose quantity
    actually changes are counted as updated. Bins with no matching item row are
    ignored (the item must be created first via invoice upload or the API).
    """
    updated = 0
    with get_conn() as conn:
        for bc in counts:
            cur = conn.execute(
                """UPDATE items
                   SET quantity = ?, updated_at = datetime('now')
                   WHERE bin_id = ? AND quantity != ?""",
                (bc.count, bc.bin_id, bc.count),
            )
            updated += cur.rowcount

        reorder = _reorder_items(conn)

    return RefreshResult(source=source, bins_seen=len(counts),
                         items_updated=updated, reorder_items=reorder)


def _reorder_items(conn) -> list[ReorderItem]:
    """Everything at/below the reorder threshold, lowest stock first."""
    rows = conn.execute(
        """SELECT item_name, color, quantity, bin_id FROM items
           WHERE quantity <= ? ORDER BY quantity, item_name""",
        (_threshold(),),
    ).fetchall()
    return [ReorderItem(**dict(r)) for r in rows]


def apply_item_counts(counts: list[ItemCount], source: str = "unoq") -> RefreshResult:
    """Reconcile per-item vision counts into the DB, then run the reorder check.

    Used when the shelf has no labelled bins: each counted item is matched to a
    stock row by name/colour (the shared fuzzy matcher, so the VLM's generic
    "Latex Balloons" finds "Helium Quality Latex Balloons"). A match has its
    quantity SET to the counted value (vision = truth); anything the camera sees
    that isn't in inventory yet is inserted with cost/rent 0 for the owner to
    price later. Items not visible in the frame are left alone — an item missing
    from one photo is not evidence the stock is gone.
    """
    from database.matching import find_stock_rows, normalize_color

    updated = created = 0
    with get_conn() as conn:
        for ic in counts:
            # columns="*": the fuzzy fallback tier reads item_name off each row.
            rows = find_stock_rows(conn, ic.item_name, ic.color)
            if rows:
                for r in rows[:1]:              # most precise match wins
                    cur = conn.execute(
                        """UPDATE items SET quantity = ?, updated_at = datetime('now')
                           WHERE id = ? AND quantity != ?""",
                        (ic.count, r["id"], ic.count),
                    )
                    updated += cur.rowcount
            else:
                conn.execute(
                    """INSERT INTO items (item_name, color, quantity, updated_at)
                       VALUES (?, ?, ?, datetime('now'))""",
                    (ic.item_name, normalize_color(ic.color), ic.count),
                )
                created += 1

        reorder = _reorder_items(conn)

    return RefreshResult(source=source, bins_seen=len(counts), items_updated=updated,
                         items_created=created, reorder_items=reorder)


def has_bins() -> bool:
    """True when the shelf is organised into labelled bins the camera can read."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM items WHERE bin_id IS NOT NULL LIMIT 1"
        ).fetchone()
    return row is not None


def refresh_from_unoq() -> RefreshResult:
    """Count the shelf with the Uno Q VLM and write the result to the shared DB.

    Uses bin counts when the DB has labelled bins, per-item counts otherwise.
    """
    if has_bins():
        return apply_counts(poll_unoq(), source="unoq")
    return apply_item_counts(poll_unoq_items(), source="unoq")


def refresh_from_arduino(source: Optional[str] = None) -> RefreshResult:
    """Full refresh cycle: get counts, then reconcile into the DB.

    With no `source`, prefers the Uno Q when its skill script is available, then
    the HTTP Arduino service when ARDUINO_URL is set, then generated dummy counts
    so the flow works end-to-end without hardware. Pass "unoq", "arduino" or
    "dummy" to force one. The result's `source` field says which was used.
    """
    if source == "unoq":
        return refresh_from_unoq()
    if source == "arduino":
        return apply_counts(poll_arduino(), source="arduino")
    if source == "dummy":
        return apply_counts(dummy_counts(), source="dummy")

    if unoq_script().is_file():
        try:
            return refresh_from_unoq()
        except Exception:
            # No board attached / VLM down: fall through to the other sources.
            # Callers that need the failure surfaced pass source="unoq".
            pass
    if os.environ.get("ARDUINO_URL"):
        return apply_counts(poll_arduino(), source="arduino")
    return apply_counts(dummy_counts(), source="dummy")
