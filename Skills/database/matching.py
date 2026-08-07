"""Shared item-matching rules for every skill that reads the inventory DB.

A vision model names things generically ("Balloon") while inventory holds
specific product names ("Golden Star Balloons"), so stock lookups need fuzzy
matching. This used to live only in inventory-management, which meant the
detection step reported an item as *present, 16 in stock* while the cost
estimate — matching exact names only — reported the same item as *0 in stock,
must buy*, in the same reply. Both now match through here.
"""
import re

_GENERIC_WORDS = {"set", "pack", "piece", "decor", "decoration", "item",
                  "large", "small", "mini"}

# Vision models emit these as a colour rather than leaving the field empty;
# passed through, they end up in Amazon queries ("not specified String Lights")
# and in the owner's shopping list.
_NULL_COLORS = {"", "not specified", "unspecified", "none", "n/a", "na",
                "unknown", "various", "assorted", "multi", "multicolor",
                "multicolour", "mixed"}


def normalize_color(color) -> "str | None":
    """The colour to actually use, or None when the model said nothing useful."""
    if not color:
        return None
    cleaned = color.strip()
    return None if cleaned.lower() in _NULL_COLORS else cleaned


def _norm_tokens(name: str) -> list:
    """Lowercase word tokens with naive plural stripping ("Balloons" -> "balloon")."""
    words = re.findall(r"[a-z]+", (name or "").lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


def names_match(detected: str, stocked: str) -> bool:
    """True when a detected name plausibly refers to a stocked item.

    Requires the head noun to agree *and* one token set to be a subset of the
    other. Both halves are load-bearing: the subset rule alone matches a bare
    colour word like "Golden" against "Golden Star Balloons", and the head noun
    alone would be too loose. "Fairy Lights" correctly does not match
    "Light Bulbs" under either.
    """
    a, b = _norm_tokens(detected), _norm_tokens(stocked)
    if not a or not b or a[-1] != b[-1]:
        return False
    small, big = (set(a), set(b)) if len(a) <= len(b) else (set(b), set(a))
    return bool(small - _GENERIC_WORDS) and small <= big


def find_stock_rows(conn, item_name: str, color=None, columns: str = "*") -> list:
    """Stock rows for an item, most precise match first.

    Three tiers: name+colour, then name alone (a photo's perceived colour often
    differs from the label), then approximate name match for the generic names a
    vision model produces. Every matching row is returned, not just the first —
    one product can occupy several rows (one per colour), and reading a single
    row understates stock.
    """
    color = normalize_color(color)
    if color:
        rows = conn.execute(
            f"""SELECT {columns} FROM items
                WHERE lower(item_name) = lower(?)
                  AND ifnull(lower(color), '') = lower(?)""",
            (item_name, color),
        ).fetchall()
        if rows:
            return rows

    rows = conn.execute(
        f"SELECT {columns} FROM items WHERE lower(item_name) = lower(?)",
        (item_name,),
    ).fetchall()
    if rows:
        return rows

    return [r for r in conn.execute(f"SELECT {columns} FROM items")
            if names_match(item_name, r["item_name"])]


def claim(consumed: dict, matched_names: list, stock: int, wanted: int) -> int:
    """Stock still available for this item once earlier items have taken theirs.

    Several detected rows routinely resolve to one inventory row (a photo yields
    "Balloon" in four colours against one "Golden Star Balloons"); without this,
    each row is measured against the full quantity and every one reads
    "present" — telling the owner to buy nothing when most are short.
    """
    key = tuple(sorted(matched_names))
    if consumed is None or not key:
        return stock
    available = max(0, stock - consumed.get(key, 0))
    consumed[key] = consumed.get(key, 0) + min(wanted, available)
    return available
