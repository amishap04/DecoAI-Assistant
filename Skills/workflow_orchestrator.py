#!/usr/bin/env python3
"""Decor idea flow, step 2 — one call from the chosen concept to a priced list.

Runs every step the owner's "I'll take concept 2" reply implies, in order,
without the agent having to hand JSON between them:

    1. detect decoration items in the chosen image     (image_cli.py)
    2. check each against the shared inventory DB      (image_cli.py)
    3. price have-vs-need for every detected item      (estimate_cli.py)
    4. turn whatever is missing into Amazon links      (amazon-url-builder)
    5. send the shopping list + links to the owner     (Telegram bot API)

The Amazon links are owner-only: they go in the Telegram message and are kept
out of stdout/JSON, which is what gets relayed to the customer.

Every step already existed as its own tool; chaining them here removes the
copy-the-JSON-into-the-next-call step, which is where the flow broke in
practice. Nothing is re-implemented -- each step shells out to the real tool,
so their behavior and error reporting stay the single source of truth.

Usage:
    workflow_orchestrator.py --concept 2 [--json]
    workflow_orchestrator.py --image <path> [--json]
    workflow_orchestrator.py --concept 2 --notify-dry-run   # print the ping, don't send
    workflow_orchestrator.py --concept 2 --no-notify        # skip the ping entirely

Step 5 goes to the owner's Telegram chat, taken from TELEGRAM_BOT_TOKEN /
TELEGRAM_OWNER_CHAT_ID, falling back to the bot token and owner id already in
~/.openclaw/openclaw.json.

--concept N resolves the image shown as concept N by the most recent
`concepts_cli.py` run (via its `concepts_latest.json` manifest), falling back to
the newest `concept<N>_*.png` in the image-generation output directory. See
AGENTS.md "DECOR IDEA FLOW".

Exit codes: 0 = success, 1 = bad input / no such concept, 2 = nothing detected,
3 = pricing, Amazon link building or the owner ping failed (detection results
are still printed).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

_SKILLS = Path(__file__).resolve().parent
load_dotenv(_SKILLS / ".env")

# The Windows console defaults to cp1252, which cannot encode the emoji in the
# owner ping (or an accented item name) -- without this, printing the report
# kills a run whose actual work already succeeded.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_IMAGE_CLI = _SKILLS / "inventory-management" / "cli" / "image_cli.py"
_ESTIMATE_CLI = _SKILLS / "cost-estimation" / "cli" / "estimate_cli.py"
_CONCEPT_DIR = _SKILLS / "image-generation" / "output"
_MANIFEST = _CONCEPT_DIR / "concepts_latest.json"
_AMAZON_DIR = _SKILLS / "amazon-url-builder"
_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Reads the missing-items array on stdin and prints the links array on stdout.
# Driving builder.js directly avoids depending on the :8004 server being up --
# buildLinks is a pure function, so the HTTP wrapper adds nothing here.
_NODE_SNIPPET = """
let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  const { buildLinks } = require('./src/builder');
  process.stdout.write(JSON.stringify(buildLinks(JSON.parse(raw))));
});
"""


def _resolve_concept(n: int) -> Path:
    """The image the owner was actually shown as concept n.

    The manifest written by concepts_cli.py is authoritative -- picking by
    filename alone would happily return a concept 3 from an earlier, unrelated
    set. Falls back to newest-by-mtime for images generated without it.
    """
    if _MANIFEST.is_file():
        try:
            manifest = json.loads(_MANIFEST.read_text())
        except json.JSONDecodeError:
            manifest = {}
        for entry in manifest.get("concepts", []):
            if entry.get("concept") == n:
                path = Path(entry.get("abs_path") or entry.get("path", ""))
                if path.is_file():
                    return path

    matches = sorted(_CONCEPT_DIR.glob(f"concept{n}_*.png"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        have = sorted({p.name.split("_")[0] for p in _CONCEPT_DIR.glob("concept*_*.png")})
        raise FileNotFoundError(
            f"no image for concept {n} in {_CONCEPT_DIR}"
            + (f" (found: {', '.join(have)})" if have else
               " -- generate concepts with --prefix concept<N> first")
        )
    return matches[0]


def _detect(image: Path, timeout: float) -> dict:
    """Run the real image CLI and return its parsed JSON payload."""
    proc = subprocess.run(
        [sys.executable, str(_IMAGE_CLI), str(image), "--json"],
        capture_output=True, text=True, timeout=timeout,
    )
    # image_cli streams its own WARNING/ERROR lines to stderr; pass them through
    # so a salvaged or failed detection stays visible rather than being swallowed.
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"item detection failed (exit {proc.returncode})")
    return json.loads(proc.stdout)


def _estimate(results: list, timeout: float) -> dict:
    """Price the whole detected list -- have vs. need per item, plus the total.

    Every detected item is passed, not just the shortfalls, so the reply can
    show what stock already covers next to what has to be bought. estimate_cli
    re-checks stock itself, so the two views stay consistent.
    """
    items = [
        {"item_name": r["item_name"], "color": r["color"],
         "quantity": r["detected_quantity"]}
        for r in results
    ]
    if not items:
        return {"lines": [], "total_cost": 0.0, "missing_items": []}
    proc = subprocess.run(
        [sys.executable, str(_ESTIMATE_CLI), json.dumps(items), "--json"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"cost estimation failed (exit {proc.returncode})")
    return json.loads(proc.stdout)


def _purchase_links(missing: list, timeout: float) -> list:
    if not missing:
        return []
    proc = subprocess.run(
        ["node", "-e", _NODE_SNIPPET],
        input=json.dumps(missing), capture_output=True, text=True,
        cwd=str(_AMAZON_DIR), timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "node exited non-zero")
    return json.loads(proc.stdout)


def _strip_urls(links: list) -> list:
    """The link list minus the URLs themselves.

    Everything this script prints can be read straight back to the customer, so
    the URLs are dropped from stdout/JSON entirely -- there is nothing to leak
    rather than a rule to remember. The full links go to the owner's bot chat.
    """
    return [{k: v for k, v in l.items() if k != "url"} for l in links]


def _telegram_config() -> tuple:
    """(bot_token, owner_chat_id) from .env, else from the OpenClaw config.

    The live bot token and the owner's chat id already exist in openclaw.json
    (`channels.telegram.botToken` and the `telegram:<id>` entry in
    `commands.ownerAllowFrom`), so falling back to it means this works without
    copying the same secret into a second file. .env still wins when set, for
    notifying a different chat than the command owner.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID") or ""
    if token and chat_id:
        return token, chat_id

    try:
        config = json.loads(_OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return token, chat_id

    if not token:
        token = (config.get("channels", {}).get("telegram", {})
                 .get("botToken") or "")
    if not chat_id:
        for entry in config.get("commands", {}).get("ownerAllowFrom", []):
            if isinstance(entry, str) and entry.startswith("telegram:"):
                chat_id = entry.split(":", 1)[1]
                break
    return token, chat_id


def _notify_text(image: Path, missing: list, estimate: dict, links: list) -> str:
    """The owner's shopping list -- what to buy, what it costs, where to buy it.

    The Amazon links live here and only here: this message goes to the owner's
    own bot chat, while the orchestrator's stdout/JSON is what ends up in front
    of the customer. See _strip_urls.
    """
    lines = [f"🛒 Decor concept confirmed: {image.name}"]
    if not missing:
        lines.append("\nEverything needed is already in stock.")
        return "\n".join(lines)

    priced = {}
    if estimate:
        for line in estimate.get("lines", []):
            priced[(line["item_name"], line.get("color"))] = line

    urls = {}
    for l in links or []:
        urls[(l["item_name"], l.get("color"))] = l["url"]

    lines.append(f"\n{len(missing)} item(s) to purchase:")
    # Telegram caps a message at 4096 chars and every line now carries a URL, so
    # the list is trimmed rather than rejected outright. The item breakdown
    # (without links) is in the chat reply anyway.
    shown = 0
    for m in missing:
        name = " ".join(x for x in (m.get("color"), m["item_name"]) if x)
        line = priced.get((m["item_name"], m.get("color")))
        cost = (f" — ${line['line_cost']:.2f}"
                if line and line["price_source"] == "db" else " — price TBD")
        entry = f"• {name} x{m['quantity']}{cost}"
        url = urls.get((m["item_name"], m.get("color")))
        if url:
            entry += f"\n  {url}"
        # 3800 leaves room for the total/footer lines appended below.
        if sum(len(x) + 1 for x in lines) + len(entry) > 3800:
            break
        lines.append(entry)
        shown += 1
    if shown < len(missing):
        lines.append(f"…and {len(missing) - shown} more")

    if estimate:
        lines.append(f"\n💰 Total (DB-priced): ${estimate['total_cost']:.2f}")
        unpriced = sum(1 for m in missing
                       if (priced.get((m["item_name"], m.get("color"))) or {})
                       .get("price_source") != "db")
        if unpriced:
            lines.append(f"({unpriced} item(s) have no DB price)")
    return "\n".join(lines)


def _notify_owner(text: str, timeout: float, dry_run: bool) -> dict:
    """Send the shopping list to the owner's Telegram chat."""
    token, chat_id = _telegram_config()
    if not token or not chat_id:
        missing_cfg = " and ".join(
            n for n, v in (("TELEGRAM_BOT_TOKEN", token),
                           ("TELEGRAM_OWNER_CHAT_ID", chat_id)) if not v)
        raise RuntimeError(f"Telegram not configured: no {missing_cfg} "
                           f"(set in Skills/.env or {_OPENCLAW_CONFIG})")
    if dry_run:
        return {"sent": False, "dry_run": True, "chat_id": chat_id, "text": text}

    import httpx      # imported here so the rest of the flow runs without it

    resp = httpx.post(_TELEGRAM_API.format(token=token),
                      json={"chat_id": chat_id, "text": text,
                            "disable_web_page_preview": True},
                      timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram API returned {resp.status_code}: {resp.text}")
    return {"sent": True, "chat_id": chat_id, "text": text}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-confirm",
        description="Chosen decor concept -> detected items -> stock check -> "
                    "cost estimate -> Amazon links -> owner Telegram ping.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--concept", type=int, help="concept number the owner confirmed")
    source.add_argument("--image", help="path to an image, if not a generated concept")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--no-notify", action="store_true",
                        help="skip the owner Telegram notification")
    parser.add_argument("--notify-dry-run", action="store_true",
                        help="resolve the Telegram config and print the message "
                             "that would be sent, without sending it")
    parser.add_argument("--timeout", type=float, default=300,
                        help="seconds allowed for detection (retries make it slow)")
    args = parser.parse_args()

    try:
        image = _resolve_concept(args.concept) if args.concept else Path(args.image)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not image.is_file():
        print(f"ERROR: no such file: {image}", file=sys.stderr)
        return 1

    try:
        detection = _detect(image, args.timeout)
    except subprocess.TimeoutExpired:
        print(f"ERROR: item detection timed out after {args.timeout}s", file=sys.stderr)
        return 2
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    missing = detection.get("missing_items", [])

    estimate, estimate_error = None, None
    try:
        estimate = _estimate(detection.get("results", []), args.timeout)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        # Detection succeeded and is worth reporting even if pricing failed, so
        # this is surfaced as a distinct exit code rather than discarding it.
        estimate_error = str(e)

    links, link_error = [], None
    try:
        links = _purchase_links(missing, args.timeout)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError,
            FileNotFoundError) as e:
        link_error = str(e)

    notify, notify_error = None, None
    if not args.no_notify:
        try:
            notify = _notify_owner(_notify_text(image, missing, estimate, links),
                                   args.timeout, args.notify_dry_run)
        except Exception as e:
            # Same rule as the steps above: a failed ping never discards the
            # work that succeeded, it just gets reported as its own failure.
            notify_error = str(e)

    payload = {
        "image": str(image),
        "items_detected": detection.get("items_detected", 0),
        "results": detection.get("results", []),
        "missing_items": missing,
        "estimate": estimate,
        # URLs deliberately omitted -- they are sent to the owner's bot chat
        # only, never into the customer-facing reply.
        "purchase_links": _strip_urls(links),
        "purchase_links_channel": "telegram-owner-only",
        "owner_notified": bool(notify and notify.get("sent")),
    }
    if estimate_error:
        payload["estimate_error"] = estimate_error
    if link_error:
        payload["purchase_links_error"] = link_error
    if notify:
        # The sent message body carries the Amazon links, so it is echoed back
        # only for --notify-dry-run, whose whole purpose is checking the wording.
        payload["notify"] = (notify if notify.get("dry_run")
                             else {k: v for k, v in notify.items() if k != "text"})
    if notify_error:
        payload["notify_error"] = notify_error

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Confirmed concept: {image.name}")
        print(f"{detection.get('items_detected', 0)} item(s) detected, "
              f"{len(missing)} need buying\n")
        for r in detection.get("results", []):
            matched = r.get("matched_item_name")
            note = (f"   [matched: {matched}]"
                    if matched and matched.lower() != r["item_name"].lower() else "")
            print(f"  {r['item_name']:<20}need {r['detected_quantity']:>3}  "
                  f"have {r['in_stock']:>3}  short {r['shortfall']:>3}  "
                  f"{r['status']}{note}")
        if estimate:
            print(f"\n{'ITEM':<20}{'NEED':>5}{'HAVE':>5}{'BUY':>5}{'COST EA':>9}"
                  f"{'LINE':>9}  SOURCE")
            for line in estimate["lines"]:
                name = " ".join(x for x in (line.get("color"), line["item_name"]) if x)
                name = name[:19]        # keep long color+name pairs in their column
                print(f"{name:<20}{line['needed']:>5}{line['in_stock']:>5}"
                      f"{line['missing']:>5}{line['cost_ea']:>9.2f}"
                      f"{line['line_cost']:>9.2f}  {line['price_source']}")
            print(f"\nTotal (DB-priced only): ${estimate['total_cost']:.2f}")
            unpriced = [l for l in estimate["lines"]
                        if l["missing"] > 0 and l["price_source"] == "missing"]
            if unpriced:
                print(f"{len(unpriced)} item(s) have no DB price - price those yourself.")
        elif estimate_error:
            print(f"\nERROR: could not price the concept: {estimate_error}", file=sys.stderr)

        if links:
            print("\nBuy:")
            for l in links:
                print(f"  - {l['search_terms']} x{l['quantity']}")
            print("(Amazon links sent to the owner's bot chat, not shown here.)")
        elif link_error:
            print(f"\nERROR: could not build Amazon links: {link_error}", file=sys.stderr)
        else:
            print("\nEverything needed is already in stock.")

        if notify and notify.get("dry_run"):
            print(f"\n[dry run] would send to chat {notify['chat_id']}:\n"
                  f"{notify['text']}")
        elif notify:
            print(f"\nOwner notified on Telegram (chat {notify['chat_id']}).")
        elif notify_error:
            print(f"\nERROR: could not notify owner: {notify_error}", file=sys.stderr)

    return 3 if (link_error or estimate_error or notify_error) else 0


if __name__ == "__main__":
    raise SystemExit(main())
