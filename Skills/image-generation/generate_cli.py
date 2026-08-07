#!/usr/bin/env python3
"""Image Generation CLI — hybrid local SD2.1 (NPU) + Cirrascale AI Suite API.

A run produces a set of MAX_IMAGES (3) images:

    image 1  -> local Stable Diffusion 2.1 on the Snapdragon NPU
                (Stable-Diffusion-2-1/generate.py, via its own venv)
    image 2+ -> Cirrascale hosted models (default: sdxl-turbo), one HTTPS
                request each, strictly sequential

Three is the whole set — `--n` is clamped to 3, never 4. Pass `--no-local` for
a cloud-only run. Requires CIRRASCALE_API_KEY in the environment (or .env at
the repo root) for the cloud images.

Always calls the API with stream=true and reads the resulting SSE event(s) —
Cirrascale's non-streaming mode (stream=false) does not return in practical
time (confirmed to hang 90s+ where streaming completes in ~15s), so this
script does not offer a non-streaming path.

Usage:
    decoai-generate-image "<prompt 1>" ["<prompt 2>" "<prompt 3>"] [options]

Give one prompt per image. Prompt 1 goes to SD2.1, so keep it short and
concrete; the cloud prompts can be fully detailed. With fewer prompts than
images, the last prompt is reused for the remaining ones.

Exit codes: 0 = every image generated, 1 = bad input / missing config,
2 = at least one backend failed (the images that succeeded are still reported),
3 = nothing was generated.
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

_SVC = Path(__file__).resolve().parent          # image-generation/
sys.path.insert(0, str(_SVC.parent))            # repo root (shared .env)

from dotenv import load_dotenv                                         # noqa: E402
load_dotenv(_SVC.parent / ".env")

import httpx                                                            # noqa: E402

API_URL = "https://aisuite.cirrascale.com/apis/v2/images/generations"
DEFAULT_MODEL = "stabilityai/sdxl-turbo"

# The delivered set is always three images — 1 local + 2 cloud. Never four.
MAX_IMAGES = 3

_SD21_DIR = _SVC / "Stable-Diffusion-2-1"
# The SD2.1 pipeline needs its own Python 3.12 ARM64 interpreter, which setup.ps1
# creates on the data drive rather than inside the skill. SD21_PYTHON points at
# it; the in-skill venv is the fallback for a hand-built tree.
_SD21_PY = Path(os.environ.get("SD21_PYTHON")
                or _SD21_DIR / "venv" / "Scripts" / "python.exe")
_SD21_SCRIPT = _SD21_DIR / "generate.py"
_SD21_SERVER = _SD21_DIR / "session_server.py"
_SD21_HOST, _SD21_PORT = "127.0.0.1", 50002


def _sd21_server_running() -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((_SD21_HOST, _SD21_PORT)) == 0


def _start_sd21_server() -> None:
    """Launch session_server.py detached; it holds the pre-loaded ORT-QNN sessions."""
    subprocess.Popen(
        [str(_SD21_PY), str(_SD21_SERVER)],
        cwd=str(_SD21_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _wait_for_sd21_server(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sd21_server_running():
            return True
        time.sleep(2.0)
    return False


def _generate_local(prompt: str, out_dir: Path, prefix: str, args) -> Path:
    """Run image 1 on the local SD2.1 NPU pipeline and return the saved path.

    Raises RuntimeError on any failure — the caller reports which backend failed
    rather than silently generating the image on Cirrascale instead.
    """
    if not _SD21_PY.exists():
        raise RuntimeError(f"SD2.1 venv python not found at {_SD21_PY}")

    if not _sd21_server_running():
        _start_sd21_server()
        if not _wait_for_sd21_server(args.local_server_timeout):
            raise RuntimeError(
                f"SD2.1 session server did not come up on {_SD21_HOST}:{_SD21_PORT} "
                f"within {args.local_server_timeout}s"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}_{int(time.time())}_sd21.png"
    cmd = [
        str(_SD21_PY), str(_SD21_SCRIPT),
        "--prompt", prompt,
        "--steps", str(args.local_steps),
        "--guidance-scale", str(args.guidance_scale),
        "--output", str(path.resolve()),
    ]
    if args.seed is not None:
        cmd += ["--seed", str(args.seed)]
    if args.negative_prompt:
        cmd += ["--negative-prompt", args.negative_prompt]

    try:
        proc = subprocess.run(cmd, cwd=str(_SD21_DIR), capture_output=True,
                              text=True, timeout=args.local_timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"SD2.1 generation timed out after {args.local_timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"generate.py exited {proc.returncode}")
    if not path.exists():
        raise RuntimeError(f"generate.py reported success but wrote no file at {path}")
    return path


def _extract_images(payload: dict) -> List[Tuple[str, str]]:
    """Pull (kind, value) pairs out of a Cirrascale response payload.

    kind is "b64_json" or "url" depending on what the API returned per item.
    """
    images = []
    for item in payload.get("data", []):
        if "b64_json" in item:
            images.append(("b64_json", item["b64_json"]))
        elif "url" in item:
            images.append(("url", item["url"]))
    return images


def _save_b64_image(b64_data: str, out_dir: Path, prefix: str, index: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}_{int(time.time())}_{index}.png"
    path.write_bytes(base64.b64decode(b64_data))
    return path


def _call_streaming(client: httpx.Client, headers: dict, body: dict, timeout: float) -> Optional[dict]:
    """POST with stream=True and return the last complete SSE JSON event.

    Observed behavior: Cirrascale sends one `data: {...}` event carrying the
    complete, fully-decoded image(s) and then closes the connection (no
    `[DONE]` sentinel seen in practice) — but a `[DONE]` line is handled too,
    in case a model/config streams multiple partial updates.
    """
    last_payload = None
    with client.stream("POST", API_URL, headers=headers, json=body, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                last_payload = json.loads(data)
            except json.JSONDecodeError:
                continue
    return last_payload


def _generate_cloud(prompt: str, out_dir: Path, prefix: str, index: int,
                    seed: Optional[int], args, api_key: str) -> Tuple[List[str], List[str]]:
    """Run one cloud image and return (saved_paths, urls). Raises RuntimeError on failure."""
    body = {
        "model": args.model,
        "prompt": prompt,
        "stream": True,
        "guidance_scale": args.guidance_scale,
        "size": args.size,
        "n": 1,
        "num_inference_steps": args.steps,
        "response_format": args.response_format,
    }
    if seed is not None:
        body["seed"] = seed
    if args.seed_increment is not None:
        body["seed_increment"] = args.seed_increment

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    verify = os.environ.get("DECOAI_INSECURE_SSL", "false").lower() != "true"

    try:
        with httpx.Client(verify=verify) as client:
            payload = _call_streaming(client, headers, body, args.timeout)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Cirrascale API returned {e.response.status_code}: {e.response.text}")
    except httpx.HTTPError as e:
        raise RuntimeError(f"request to Cirrascale API failed: {e}")

    if not payload:
        raise RuntimeError("no response payload from Cirrascale API")
    images = _extract_images(payload)
    if not images:
        raise RuntimeError(f"no image data in response: {payload}")

    saved_paths, urls = [], []
    for kind, value in images:
        if kind == "b64_json":
            saved_paths.append(str(_save_b64_image(value, out_dir, prefix, index)))
        else:
            urls.append(value)
    return saved_paths, urls


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-generate-image",
        description="Generate a 3-image set: image 1 on the local SD2.1 NPU, "
                    "the rest via the Cirrascale AI Suite images API.",
    )
    parser.add_argument("prompts", nargs="+",
                        help="one prompt per image (prompt 1 is the SD2.1 one — keep it "
                             "short and concrete); the last prompt is reused if you pass "
                             "fewer prompts than images")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--size", default=None)
    parser.add_argument("--n", type=int, default=MAX_IMAGES,
                        help=f"total images in the set (clamped to {MAX_IMAGES})")
    parser.add_argument("--no-local", action="store_true",
                        help="skip the SD2.1 NPU image and run every image on Cirrascale")
    parser.add_argument("--negative-prompt", default="", help="SD2.1 negative prompt")
    parser.add_argument("--local-steps", type=int, default=20, choices=[20, 50],
                        help="denoising steps for the SD2.1 image")
    parser.add_argument("--local-timeout", type=float, default=900,
                        help="seconds allowed for the SD2.1 image")
    parser.add_argument("--local-server-timeout", type=float, default=300,
                        help="seconds to wait for the SD2.1 session server to load")
    parser.add_argument("--steps", type=int, default=50, help="num_inference_steps")
    parser.add_argument("--guidance-scale", type=float, default=7.5)
    parser.add_argument("--seed", type=int, default=4)
    parser.add_argument("--seed-increment", type=int, default=None)
    parser.add_argument("--response-format", default="b64_json", choices=["b64_json", "url"])
    parser.add_argument("--output-dir", default="Skills/image-generation/output",
                        help="workspace-relative by default, so saved_paths can be embedded "
                             "directly as markdown image links")
    parser.add_argument("--prefix", default="cirrascale")
    parser.add_argument("--timeout", type=float, default=90,
                        help="request timeout in seconds (cold-start GPU calls can take ~15-60s)")
    parser.add_argument("--json", action="store_true", help="emit a JSON result summary")
    args = parser.parse_args()

    # Three is the whole set. A caller asking for four gets three.
    total = max(1, min(args.n, MAX_IMAGES))
    if args.n > MAX_IMAGES:
        print(f"NOTE: --n {args.n} capped at {MAX_IMAGES} images", file=sys.stderr)
    if len(args.prompts) > total:
        print(f"NOTE: {len(args.prompts)} prompts given, only the first {total} are used",
              file=sys.stderr)

    def prompt_for(i: int) -> str:
        return args.prompts[i] if i < len(args.prompts) else args.prompts[-1]

    use_local = not args.no_local
    cloud_count = total - 1 if use_local else total

    api_key = os.environ.get("CIRRASCALE_API_KEY")
    if cloud_count > 0 and not api_key:
        print("ERROR: CIRRASCALE_API_KEY is not set (add it to .env)", file=sys.stderr)
        return 1

    out_dir = Path(args.output_dir)
    saved_paths, urls, images_meta, errors = [], [], [], []

    if use_local:
        try:
            path = _generate_local(prompt_for(0), out_dir, args.prefix, args)
        except RuntimeError as e:
            # Named explicitly: the owner needs to know the NPU image is the one
            # missing. It is never silently regenerated on Cirrascale.
            print(f"ERROR: local SD2.1 (NPU) image failed: {e}", file=sys.stderr)
            errors.append({"image": 1, "backend": "sd2.1-npu", "error": str(e)})
        else:
            saved_paths.append(str(path))
            images_meta.append({"image": 1, "backend": "sd2.1-npu",
                                "model": "stable-diffusion-2-1",
                                "prompt": prompt_for(0), "path": str(path)})

    # Cloud images stay strictly sequential — Cirrascale drops concurrent requests.
    for offset in range(cloud_count):
        index = offset + 1 if use_local else offset
        seed = args.seed + index if args.seed is not None else None
        try:
            paths, found_urls = _generate_cloud(
                prompt_for(index), out_dir, args.prefix, index, seed, args, api_key)
        except RuntimeError as e:
            print(f"ERROR: cloud image {index + 1} failed: {e}", file=sys.stderr)
            errors.append({"image": index + 1, "backend": "cirrascale", "error": str(e)})
            continue
        saved_paths.extend(paths)
        urls.extend(found_urls)
        images_meta.append({"image": index + 1, "backend": "cirrascale",
                            "model": args.model, "prompt": prompt_for(index),
                            "path": paths[0] if paths else None,
                            "url": found_urls[0] if found_urls else None})

    if args.json:
        print(json.dumps({"saved_paths": saved_paths, "urls": urls,
                          "images": images_meta, "errors": errors}))
    else:
        for m in images_meta:
            print(f"Image {m['image']} [{m['backend']}]: {m['path'] or m.get('url')}")

    if not saved_paths and not urls:
        return 3
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
