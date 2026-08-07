#!/usr/bin/env python3
"""
Ask the Uno Q VLM from your Mac.

Examples:
  # Capture from ESP32-CAM, then ask
  ./scripts/ask_vlm.py "How many balloons are in this photo?"

  # Capture, ask, and save the photo locally
  ./scripts/ask_vlm.py --save -- "How many balloons are in this photo?"
  ./scripts/ask_vlm.py --save my_shot.jpg "How many balloons?"

  # Use a local image
  ./scripts/ask_vlm.py --image esp32_cam/s-l400.jpg "What do you see?"

  # Reuse last frame already on the board
  ./scripts/ask_vlm.py --latest "Describe this image."
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BOARD_LATEST = "/home/arduino/vlm/latest.jpg"
BOARD_CAPTURE_SCRIPT = "/home/arduino/capture_esp32_ap.sh"
MODEL = "Qwen3.5-2B-Q4_K_M"
DEFAULT_PROMPT = "What do you see in this photo? Reply in one or two short sentences."



def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def adb_shell(script: str) -> str:
    result = run(["adb", "shell", script], capture_output=True)
    return result.stdout


def ensure_adb() -> None:
    try:
        out = run(["adb", "devices"], capture_output=True).stdout
    except FileNotFoundError as exc:
        raise SystemExit("adb not found. Install: brew install android-platform-tools") from exc
    lines = [l for l in out.splitlines()[1:] if l.strip() and "device" in l]
    if not lines:
        raise SystemExit("No Uno Q found. Connect USB and check: adb devices")


def capture_from_esp32() -> None:
    print("Capturing from ESP32-CAM-MB …", file=sys.stderr)
    run(["adb", "shell", f"bash {BOARD_CAPTURE_SCRIPT}"])


def push_image(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"Image not found: {path}")
    print(f"Pushing {path.name} → Uno Q …", file=sys.stderr)
    adb_shell(f"mkdir -p $(dirname {BOARD_LATEST})")
    run(["adb", "push", str(path), BOARD_LATEST], capture_output=True)


def pull_latest_to_temp() -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".jpg")[1])
    run(["adb", "pull", BOARD_LATEST, str(tmp)], capture_output=True)
    return tmp


def ask_vlm(prompt: str, image_path: Path, timeout: int = 600) -> str:
    prompt = r"This photo shows party and event decoration inventory. For every distinct item you can see, output one JSON object with the item's name written out in words, and how many of that item are visible. Output format - a JSON array, exactly like this: [{\"item_name\": \"Latex Balloons\", \"color\": \"blue\", \"count\": 12}]. Rules: item_name must be the item's name in words, never a number, letter, or code. Output a JSON array of objects, do NOT output an object with numeric keys like {\"1\": 4, \"2\": 3}, do NOT number the items. Use null for color if it is unclear. Only list items you can actually see. No prose, no markdown fences."
    """Forward llama port and call OpenAI-compatible chat API."""
    # Clean any old forward, then map board :9999 → local :19999
    subprocess.run(["adb", "forward", "--remove", "tcp:19999"], capture_output=True)
    run(["adb", "forward", "tcp:19999", "tcp:9999"], capture_output=True)

    jpeg = image_path.read_bytes()
    b64 = base64.b64encode(jpeg).decode()
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a vision assistant. Answer the user's question about "
                    "the image clearly and concisely. Do not reply with only a number "
                    "unless the user asks for a number."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            },
        ],
        "max_tokens": 256,
        "temperature": 0.3,
    }

    print("Asking Qwen on Uno Q (may take 1–3+ minutes) …", file=sys.stderr)
    req = __import__("urllib.request").request.Request(
        "http://127.0.0.1:19999/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with __import__("urllib.request").request.urlopen(req, timeout=timeout) as resp:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture + ask Uno Q VLM from your Mac")
    parser.add_argument(
        "prompt",
        nargs="?",
        default=DEFAULT_PROMPT,
        help="Question to ask about the image",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--image",
        "-i",
        type=Path,
        help="Local JPEG/PNG to upload and ask about",
    )
    src.add_argument(
        "--latest",
        action="store_true",
        help="Use latest.jpg already on the Uno Q (skip capture)",
    )
    src.add_argument(
        "--capture",
        action="store_true",
        default=False,
        help="Capture from ESP32-CAM soft-AP (default if no --image/--latest)",
    )
    parser.add_argument(
        "--save",
        "-o",
        nargs="?",
        const="auto",
        default=None,
        metavar="PATH",
        help=(
            "Save the frame used for the ask to PATH. "
            "If PATH is omitted, writes ./capture_YYYYMMDD_HHMMSS.jpg"
        ),
    )
    args = parser.parse_args()

    ensure_adb()

    # Default action: capture from ESP32 unless --image or --latest
    if args.image:
        push_image(args.image)
    elif args.latest:
        print("Using latest frame on Uno Q …", file=sys.stderr)
        # Verify it exists
        check = adb_shell(f"test -f {BOARD_LATEST} && echo OK || echo MISSING").strip()
        if check != "OK":
            raise SystemExit("No latest.jpg on board. Capture first or pass --image.")
    else:
        capture_from_esp32()

    local = pull_latest_to_temp()
    try:
        if args.save is not None:
            if args.save == "auto":
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out = Path(f"capture_{stamp}.jpg")
            else:
                out = Path(args.save)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, out)
            print(f"Saved frame → {out.resolve()}", file=sys.stderr)

        answer = ask_vlm(args.prompt, local)
    finally:
        local.unlink(missing_ok=True)

    print(answer)


if __name__ == "__main__":
    main()
