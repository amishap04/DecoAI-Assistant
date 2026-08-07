"""Cost estimation logic — Diagram 1.

Given the items a decoration concept needs, look up current stock in the shared
inventory DB and produce an itemized estimate: what's already on the shelf vs.
what must be bought. Prices come only from the DB — items missing a known cost
are reported with cost_ea=0 and price_source="missing"; pricing those is left to
the OpenClaw agent's own LLM (which can get real prices), fed by missing_items.
"""
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

# Make the shared database/ package importable regardless of launch dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from database.db import get_conn  # noqa: E402
from database.matching import claim, find_stock_rows, normalize_color  # noqa: E402


class NeededItem(BaseModel):
    """One item a decoration concept requires."""
    item_name: str
    color: Optional[str] = None
    quantity: int = Field(1, ge=0)


class EstimateLine(BaseModel):
    """Per-item breakdown of have vs. need."""
    item_name: str
    color: Optional[str] = None
    needed: int
    in_stock: int          # units already on the shelf (capped at needed)
    missing: int           # units that must be purchased
    cost_ea: float         # unit purchase price (0 if unknown — see price_source)
    line_cost: float       # missing * cost_ea — the cost to fulfill this item
    price_source: str      # "db" or "missing" (no known cost; not priced here)


class Estimate(BaseModel):
    lines: list[EstimateLine]
    total_cost: float                 # sum of line_cost across all items
    missing_items: list[NeededItem]   # feed straight into the Amazon URL builder


def estimate(items: list[NeededItem]) -> Estimate:
    """Build an itemized cost estimate for the needed items.

    Stock is matched through database.matching -- the same rules the image
    analysis uses. Matching exact names only here meant the two halves of one
    reply disagreed: detection called an item present with 16 in stock while
    this priced the same item as 0 in stock and told the owner to buy it.
    """
    lines: list[EstimateLine] = []
    missing_items: list[NeededItem] = []
    total = 0.0
    consumed = {}       # stock claimed so far, shared across all items

    with get_conn() as conn:
        for item in items:
            color = normalize_color(item.color)
            rows = find_stock_rows(conn, item.item_name, color,
                                   columns="item_name, quantity, cost_ea")

            stock_qty = sum(r["quantity"] for r in rows)
            # Cheapest matching row with a real price: several rows can back one
            # item, and a 0 there means "unpriced", not "free".
            costs = [r["cost_ea"] for r in rows if r["cost_ea"]]
            cost_ea = min(costs) if costs else 0.0
            price_source = "db" if cost_ea > 0 else "missing"

            stock_qty = claim(consumed, sorted({r["item_name"] for r in rows}),
                              stock_qty, item.quantity)
            in_stock = min(stock_qty, item.quantity)
            missing = item.quantity - in_stock

            line_cost = round(missing * cost_ea, 2)
            total += line_cost

            lines.append(EstimateLine(
                item_name=item.item_name,
                color=color,
                needed=item.quantity,
                in_stock=in_stock,
                missing=missing,
                cost_ea=cost_ea,
                line_cost=line_cost,
                price_source=price_source,
            ))
            if missing > 0:
                missing_items.append(NeededItem(
                    item_name=item.item_name,
                    color=color,
                    quantity=missing,
                ))

    return Estimate(
        lines=lines,
        total_cost=round(total, 2),
        missing_items=missing_items,
    )
