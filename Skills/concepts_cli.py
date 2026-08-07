#!/usr/bin/env python3
"""Decor idea flow, step 1 — one call that generates the whole concept set.

The owner asks for decor ideas; they should get several images back. Cirrascale
drops concurrent image requests (parallel calls answer HTTP 200 with an empty
event stream), so the concepts have to be generated one after another. Doing
that as N separate agent tool calls is where the flow stalls in practice — this
runs the same generator sequentially inside a single call instead.

Nothing is re-implemented: each concept shells out to generate_cli.py, so the
API behavior, retries and error text stay owned by that script.

Each concept N is saved with prefix `conceptN`, and the run is recorded in
`concepts_latest.json` next to the images so `workflow_orchestrator.py
--concept N` resolves exactly the image the owner was shown, not an older
same-numbered leftover.

Usage:
    concepts_cli.py "<prompt 1>" "<prompt 2>" "<prompt 3>" --json

The set is three concepts, never four. Concept 1 runs on the local SD2.1 NPU
pipeline (keep that prompt short and concrete); concepts 2 and 3 run on
Cirrascale — expand those prompts fully, one per distinct concept.

Exit codes: 0 = every concept generated, 2 = some failed (the rest are still
reported), 3 = all failed.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_SKILLS = Path(__file__).resolve().parent
_WORKSPACE = _SKILLS.parent
_GENERATE_CLI = _SKILLS / "image-generation" / "generate_cli.py"
_OUTPUT_DIR = _SKILLS / "image-generation" / "output"
_MANIFEST = _OUTPUT_DIR / "concepts_latest.json"

# The concept set is three images (1 local SD2.1 + 2 Cirrascale), never four.
MAX_CONCEPTS = 3


def _rel(path: Path) -> str:
    """Workspace-relative form, which is what a markdown image link needs."""
    try:
        return path.resolve().relative_to(_WORKSPACE).as_posix()
    except ValueError:
        return str(path)


def _generate(prompts, args) -> dict:
    """Run the whole concept set in one generate_cli call and return its JSON.

    generate_cli.py owns the split (concept 1 on the local SD2.1 NPU, the rest
    on Cirrascale) and the sequencing, so nothing is duplicated here.
    """
    cmd = [
        sys.executable, str(_GENERATE_CLI), *prompts,
        "--n", str(len(prompts)),
        "--prefix", "concept",
        "--output-dir", str(args.output_dir),
        "--size", args.size,
        "--steps", str(args.steps),
        "--json",
    ]
    if args.model:
        cmd += ["--model", args.model]
    if args.seed is not None:
        cmd += ["--seed", str(args.seed)]
    if args.no_local:
        cmd += ["--no-local"]

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=args.timeout * len(prompts))
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or f"generate_cli exited {proc.returncode}")
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="decoai-concepts",
        description="Generate a set of decor concept images sequentially, one call.",
    )
    parser.add_argument("prompts", nargs="+",
                        help="one image prompt per concept (3 is the whole set; prompt 1 "
                             "runs on the local SD2.1 NPU, so keep it short and concrete)")
    parser.add_argument("--no-local", action="store_true",
                        help="run every concept on Cirrascale instead of splitting")
    parser.add_argument("--model", default=None)
    parser.add_argument("--size", default="512x512")
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output-dir", default=str(_OUTPUT_DIR))
    parser.add_argument("--timeout", type=float, default=120,
                        help="seconds allowed per concept (each takes ~25s)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()
    args.output_dir = Path(args.output_dir)

    prompts = args.prompts[:MAX_CONCEPTS]
    if len(args.prompts) > MAX_CONCEPTS:
        print(f"NOTE: {len(args.prompts)} prompts given, only the first {MAX_CONCEPTS} "
              f"are used — the set is always {MAX_CONCEPTS} concepts", file=sys.stderr)

    concepts, errors = [], []
    try:
        result = _generate(prompts, args)
    except (RuntimeError, json.JSONDecodeError) as e:
        result = None
        errors.append({"concept": 0, "error": str(e)})
    except subprocess.TimeoutExpired:
        result = None
        errors.append({"concept": 0, "error": f"timed out after {args.timeout}s per concept"})

    if result:
        errors.extend({"concept": e.get("image"), "backend": e.get("backend"),
                       "error": e.get("error")} for e in result.get("errors", []))
        for meta in result.get("images", []):
            if not meta.get("path"):
                continue
            path = Path(meta["path"])
            concepts.append({
                "concept": meta["image"],
                "backend": meta["backend"],
                "prompt": meta["prompt"],
                "path": _rel(path),
                "abs_path": str(path.resolve()),
            })

    if concepts:
        # Recorded so a later "--concept N" resolves this run's image even if
        # older conceptN_*.png files are still sitting in the output dir.
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _MANIFEST.write_text(json.dumps(
            {"generated_at": int(time.time()), "concepts": concepts}, indent=2))

    result = {"concepts": concepts, "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for c in concepts:
            print(f"Concept {c['concept']} [{c['backend']}]: {c['path']}")
        for e in errors:
            print(f"Concept {e['concept']}: FAILED - {e['error']}", file=sys.stderr)

    if not concepts:
        return 3
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
