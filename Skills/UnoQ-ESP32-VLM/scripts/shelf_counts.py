#!/usr/bin/env python3
"""Shelf counts from the Uno Q VLM.

Captures a shelf frame (ESP32-CAM by default), asks Qwen on the Uno Q to count
what it sees, and prints JSON on stdout. Two modes:

    items (default)  [{"item_name": "Latex Balloons", "color": "blue", "count": 12}, ...]
                     — whatever the camera actually sees, no bin labels needed
    bins             [{"bin_id": "A1", "count": 42}, ...]
                     — only when the shelf has labelled bins recorded in the DB

`auto` picks bins when the DB has bin ids (or --bins is given), items otherwise.
This script never writes to the DB — `refresh_cli.py --unoq` does that.

Usage:
    shelf_counts.py [--mode auto|items|bins] [--bins "A1=red balloon,A2=gold streamer"]
                    [--image PATH | --latest] [--save [PATH]] [--raw]

Exit codes: 0 = success, 1 = board/VLM failure, 2 = reply not parseable,
            3 = the VLM saw nothing in the frame.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                      # ask_vlm.py lives here
sys.path.insert(0, str(_HERE.parents[1]))           # Skills/ root (has database/)

# Frames land here by default so the owner can be shown the image, not a path.
# Workspace-relative, matching the image-generation skill's output convention.
OUTPUT_DIR = _HERE.parents[1] / "UnoQ-ESP32-VLM" / "output"

import ask_vlm as vlm                                                  # noqa: E402


def bins_from_db() -> list[tuple[str, str]]:
    """(bin_id, label) for every bin in the shared inventory DB."""
    from database.db import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT bin_id, color, item_name FROM items
               WHERE bin_id IS NOT NULL ORDER BY bin_id"""
        ).fetchall()

    seen: dict[str, str] = {}
    for r in rows:
        label = " ".join(x for x in (r["color"], r["item_name"]) if x)
        seen.setdefault(r["bin_id"], label)
    return list(seen.items())


def parse_bins_arg(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        bin_id, _, label = chunk.partition("=")
        pairs.append((bin_id.strip(), label.strip()))
    return pairs


def build_prompt(bins: list[tuple[str, str]]) -> str:
    listing = "\n".join(
        f"- {bin_id}: {label}" if label else f"- {bin_id}" for bin_id, label in bins
    )
    return (
        "This is a photo of a storage shelf. Each bin is labelled with an id.\n"
        "Count how many items are in each of these bins:\n"
        f"{listing}\n\n"
        "Reply with ONLY a JSON array, no prose, no markdown fence, in exactly this "
        'form: [{"bin_id": "A1", "count": 12}]. Use 0 for a bin you can see but that '
        "is empty. Omit a bin entirely if it is not visible in the photo."
    )


def ask_structured(prompt: str, image_path: Path, timeout: int = 900) -> str:
    """Ask the board's VLM with sampling tuned for JSON instead of prose.

    ask_vlm.ask_vlm() is tuned for free-form answers (temperature 0.3, 256
    tokens, no repeat penalty). At those settings the Q4 2B model reliably locks
    into a repeat loop on list-shaped tasks — the same item emitted over and
    over until max_tokens guillotines the reply mid-object. Structured counting
    needs a colder, longer, repetition-penalised draw, so this sends its own
    payload rather than changing the behaviour of the ad-hoc ask path.
    """
    subprocess.run(["adb", "forward", "--remove", "tcp:19999"], capture_output=True)
    subprocess.run(["adb", "forward", "tcp:19999", "tcp:9999"],
                   capture_output=True, check=True)

    b64 = base64.b64encode(image_path.read_bytes()).decode()
    payload = {
        "model": vlm.MODEL,
        "messages": [
            {"role": "system",
             "content": "You are a vision assistant that replies with JSON only."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
        "max_tokens": 512,        # 256 truncates mid-array as soon as it loops
        "temperature": 0.1,       # format stability matters more than variety
        "repeat_penalty": 1.15,   # the actual fix for the repeat loop
        "stop": ["]"],            # stop at the end of the array, not mid-ramble
    }

    print("Asking Qwen on Uno Q (may take 1-3+ minutes) ...", file=sys.stderr)
    req = urllib.request.Request(
        "http://127.0.0.1:19999/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as exc:
        raise SystemExit(
            f"VLM request failed: {exc}\n"
            "Is the VLM running? Try: adb shell 'bash ~/start_vlm.sh'"
        ) from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise SystemExit(f"Unexpected VLM response: {data}") from exc


def known_item_names() -> list[str]:
    """Item names already in the shared DB — hints so the VLM reuses our wording."""
    from database.db import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT color, item_name FROM items ORDER BY item_name"
        ).fetchall()
    return [" ".join(x for x in (r["color"], r["item_name"]) if x) for r in rows]


def build_items_prompt(known: list[str]) -> str:
    """The counting prompt.

    Deliberately never presents the known items as a numbered or bulleted list:
    shown one, the model answers in those labels instead of item names, which is
    where {"1": 4, "2": 3} replies come from. Names go inline, semicolon-joined.
    """
    hint = ""
    if known:
        hint = ("If an item matches one of these known items, reuse that exact "
                "wording: " + "; ".join(known) + ".\n\n")

    return (
        "This photo shows party and event decoration inventory.\n\n"
        "For every distinct item you can see, output one JSON object with the item's "
        "name written out in words, and how many of that item are visible.\n\n"
        f"{hint}"
        "Output format - a JSON array, exactly like this:\n"
        '[{"item_name": "Latex Balloons", "color": "blue", "count": 12},\n'
        ' {"item_name": "Pillar Candles", "color": "white", "count": 3}]\n\n'
        "Rules:\n"
        '- "item_name" must be the item\'s name in words, never a number, letter, or code.\n'
        "- Output a JSON array of objects. Do NOT output an object with numeric keys "
        'like {"1": 4, "2": 3}. Do NOT number the items.\n'
        "- List each distinct item exactly once. Never repeat an item.\n"
        '- Use null for "color" if it is unclear.\n'
        "- Only list items you can actually see. No prose, no markdown fences."
    )


def _first_json_array(reply: str):
    match = re.search(r"\[.*\]", reply, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _as_count(value) -> "int | None":
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _salvage_objects(reply: str) -> list:
    """Recover whole {...} objects from a reply that isn't valid JSON overall.

    Two things routinely break a strict parse: the model loops on one item until
    max_tokens cuts it off mid-object, leaving an unterminated array; and the
    "]" stop token means a well-behaved reply has no closing bracket either.
    Both still contain complete, individually-parseable objects up to the cut.
    """
    start = reply.find("[")
    body = reply[start:] if start != -1 else reply

    objects = []
    for chunk in re.findall(r"\{[^{}]*\}", body):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _dedupe(items: list[dict]) -> list[dict]:
    """Collapse a repeat loop's output to one entry per item.

    A degenerate reply repeats the same item dozens of times; taking the highest
    count keeps a genuine "12 balloons" over a truncated "1" from the same run,
    and real distinct items are unaffected.
    """
    best: dict = {}
    for item in items:
        key = (item["item_name"].strip().lower(),
               (item["color"] or "").strip().lower())
        if key not in best or item["count"] > best[key]["count"]:
            best[key] = item
    return list(best.values())


def parse_item_counts(reply: str) -> list[dict]:
    """Pull {item_name, color, count} objects out of a reply.

    Tries a strict array parse first, then falls back to salvaging individual
    objects from a truncated or looped reply.
    """
    data = _first_json_array(reply)
    if not data:
        data = _salvage_objects(reply)
    if not data:
        return []

    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("item_name") or entry.get("item") or entry.get("name")
        count = _as_count(entry.get("count", entry.get("quantity")))
        if not name or count is None:
            continue
        color = entry.get("color")
        items.append({
            "item_name": str(name).strip(),
            "color": str(color).strip() if color else None,
            "count": count,
        })
    return _dedupe(items)


def parse_counts(reply: str) -> list[dict]:
    """Pull the first JSON array of {bin_id, count} objects out of a VLM reply."""
    data = _first_json_array(reply)
    if not data:
        return []

    counts = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        bin_id = entry.get("bin_id") or entry.get("bin") or entry.get("id")
        count = _as_count(entry.get("count", entry.get("quantity")))
        if bin_id is None or count is None:
            continue
        counts.append({"bin_id": str(bin_id).strip(), "count": count})
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="shelf_counts",
        description="Shelf counts from the Uno Q VLM (JSON on stdout).",
    )
    parser.add_argument("--mode", choices=("auto", "items", "bins"), default="auto",
                        help="count per item (default when there are no bins) or per bin")
    parser.add_argument("--bins", help='"A1=red balloon,A2=gold streamer" (implies --mode bins)')
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--image", type=Path, help="count from a local photo")
    src.add_argument("--latest", action="store_true", help="reuse the frame on the board")
    parser.add_argument("--save", nargs="?", const="auto", default=None, metavar="PATH",
                        help="save the frame used for counting")
    parser.add_argument("--raw", action="store_true", help="echo the raw VLM reply to stderr")
    parser.add_argument("--json", action="store_true",
                        help="with --capture-only, emit the saved paths as JSON")
    parser.add_argument("--capture-only", action="store_true",
                        help="capture a frame, save it locally, print its path and stop "
                             "(no VLM call) so it can be reviewed before counting")
    args = parser.parse_args()

    # Capture-and-stop: the frame is for a human to look at before anything is
    # counted or written. Counting it afterwards is `--latest`, which reuses
    # this exact frame rather than taking a new one.
    if args.capture_only:
        try:
            vlm.ensure_adb()
            if args.image:
                vlm.push_image(args.image)
            elif not args.latest:
                vlm.capture_from_esp32()
            local = vlm.pull_latest_to_temp()
        except SystemExit as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        out = (OUTPUT_DIR / f"frame_{datetime.now():%Y%m%d_%H%M%S}.jpg"
               if args.save in (None, "auto") else Path(args.save))
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, out)
        # Stable path for "show me the latest frame" queries, alongside the
        # timestamped copy that keeps the history.
        latest = OUTPUT_DIR / "latest.jpg"
        if out != latest:
            shutil.copy2(out, latest)
        try:
            local.unlink(missing_ok=True)
        except PermissionError:
            pass

        size = out.stat().st_size
        if args.json:
            print(json.dumps({"saved_path": str(out.resolve()),
                              "latest_path": str(latest.resolve()),
                              "bytes": size,
                              "likely_blank": size < 4000}, indent=2))
            return 0
        print(out.resolve())
        if size < 4000:
            print(f"WARNING: frame is only {size} bytes — likely blank, dark or "
                  "occluded; check the camera before counting", file=sys.stderr)
        return 0

    mode = args.mode
    bins: list[tuple[str, str]] = []
    if mode in ("auto", "bins"):
        bins = parse_bins_arg(args.bins) if args.bins else bins_from_db()
        if bins:
            mode = "bins"
        elif mode == "bins":
            print("ERROR: no bins to count (empty --bins and no bins in the DB)",
                  file=sys.stderr)
            return 2
        else:
            mode = "items"

    prompt = build_prompt(bins) if mode == "bins" else build_items_prompt(known_item_names())

    try:
        vlm.ensure_adb()
        if args.image:
            vlm.push_image(args.image)
        elif args.latest:
            check = vlm.adb_shell(
                f"test -f {vlm.BOARD_LATEST} && echo OK || echo MISSING"
            ).strip()
            if check != "OK":
                print("ERROR: no latest.jpg on board; capture first or pass --image",
                      file=sys.stderr)
                return 1
        else:
            vlm.capture_from_esp32()

        local = vlm.pull_latest_to_temp()
        try:
            if args.save is not None:
                out = (Path(f"shelf_{datetime.now():%Y%m%d_%H%M%S}.jpg")
                       if args.save == "auto" else Path(args.save))
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local, out)
                print(f"Saved frame → {out.resolve()}", file=sys.stderr)
            reply = ask_structured(prompt, local)
        finally:
            try:
                local.unlink(missing_ok=True)
            except PermissionError:
                # Windows keeps the mkstemp handle open; the temp file is harmless.
                pass
    except SystemExit as exc:                    # ask_vlm raises SystemExit on hardware faults
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.raw:
        print(reply, file=sys.stderr)

    counts = parse_counts(reply) if mode == "bins" else parse_item_counts(reply)
    if not counts:
        # An empty array is a real answer, not a parse failure: the model saw
        # nothing countable. With stop=["]"] it arrives as a bare "[".
        if reply.strip() in ("[", "[]"):
            print("ERROR: the VLM saw no items in the frame — check the camera is "
                  "pointed at the inventory and the shot is not blank or dark",
                  file=sys.stderr)
            return 3
        print(f"ERROR: could not parse counts from VLM reply: {reply!r}", file=sys.stderr)
        return 2

    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
