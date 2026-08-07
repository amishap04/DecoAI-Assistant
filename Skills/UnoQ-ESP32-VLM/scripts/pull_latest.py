#!/usr/bin/env python3
"""Pull the latest camera frame off the Uno Q and save it for display.

Answers "show me the latest image / what does the camera see" without spending
1-3 minutes on a VLM call, and without re-capturing: it copies the frame already
on the board (/home/arduino/vlm/latest.jpg) into the skill's output folder.

Pass --capture to grab a fresh frame from the ESP32-CAM first (~20s).

Usage:
    pull_latest.py [--capture] [--save PATH] [--json]

Prints the saved path on stdout; embed it in the reply as a markdown image so
the owner sees the frame, not a file path.

Exit codes: 0 = saved, 1 = board unreachable / no frame on board.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                      # ask_vlm.py lives here

import ask_vlm as vlm                                                  # noqa: E402

OUTPUT_DIR = _HERE.parent / "output"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pull_latest",
        description="Save the Uno Q's latest camera frame for display.",
    )
    parser.add_argument("--capture", action="store_true",
                        help="capture a fresh frame from the ESP32-CAM first (~20s)")
    parser.add_argument("--save", metavar="PATH",
                        help="where to save (default: output/frame_<timestamp>.jpg)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a path")
    args = parser.parse_args()

    try:
        vlm.ensure_adb()
        if args.capture:
            vlm.capture_from_esp32()
        else:
            check = vlm.adb_shell(
                f"test -f {vlm.BOARD_LATEST} && echo OK || echo MISSING"
            ).strip()
            if check != "OK":
                print("ERROR: no frame on the board yet — run with --capture",
                      file=sys.stderr)
                return 1
        local = vlm.pull_latest_to_temp()
    except SystemExit as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    out = (Path(args.save) if args.save
           else OUTPUT_DIR / f"frame_{datetime.now():%Y%m%d_%H%M%S}.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, out)

    latest = OUTPUT_DIR / "latest.jpg"          # stable path for repeat queries
    latest.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve() != latest.resolve():
        shutil.copy2(out, latest)

    try:
        local.unlink(missing_ok=True)
    except PermissionError:                     # Windows holds the mkstemp handle
        pass

    size = out.stat().st_size
    if args.json:
        print(json.dumps({"saved_path": str(out.resolve()),
                          "latest_path": str(latest.resolve()),
                          "bytes": size,
                          "likely_blank": size < 4000}, indent=2))
    else:
        print(out.resolve())
    if size < 4000:
        print(f"WARNING: frame is only {size} bytes — likely blank, dark or occluded",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
