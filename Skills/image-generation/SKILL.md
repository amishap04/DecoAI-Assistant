---
name: image-generation
description: Generate images via the local SD2.1 NPU pipeline and the Cirrascale AI Suite cloud API (text-to-image), and save/embed the results. `generate_cli.py` performs the hybrid split itself — every set is 3 images, image 1 on local SD2.1 (NPU) and images 2-3 on Cirrascale SDXL. Use for any decor, theme, or setup idea request — decor ideas are always delivered as generated images, not text-only descriptions.
metadata:
  { "openclaw": { "primaryEnv": "CIRRASCALE_API_KEY", "emoji": "🖼️" } }
---

# SKILL.md — image-generation

## Overview

| Field       | Details                                                                 |
|-------------|--------------------------------------------------------------------------|
| **Name**    | image-generation                                                         |
| **Type**    | Text-to-Image Generation (Cirrascale AI Suite cloud API)                 |
| **Script**  | `generate_cli.py` (cloud) · `Stable-Diffusion-2-1/generate.py` (local NPU) |
| **Backend** | `https://aisuite.cirrascale.com/apis/v2/images/generations` · local Snapdragon NPU (ORT-QNN) |

**Description:**
`generate_cli.py` produces a **3-image set** in one call:

| Image | Backend | Model |
|-------|---------|-------|
| 1 | Local Snapdragon NPU (ORT-QNN) | Stable Diffusion 2.1 |
| 2 | Cirrascale cloud | `stabilityai/sdxl-turbo` |
| 3 | Cirrascale cloud | `stabilityai/sdxl-turbo` |

The script owns the split, the session-server startup, and the sequencing —
you do not hand-orchestrate the backends. Three is the whole set: `--n` is
clamped to 3, and extra prompts are ignored. Pass `--no-local` for a
cloud-only run.

This skill takes a prompt as-is — it does not expand or embellish it. Turning
the owner's short request into a detailed, vivid image-generation prompt is
the calling agent's job, done before invoking this CLI (see `AGENTS.md` →
"CLOUD IMAGE GENERATION").

---

## ⚠️ CRITICAL

> Requires `CIRRASCALE_API_KEY` set in `.env` (repo root). Never hardcode the
> key in a script, prompt, or commit — it's a live bearer credential.
> Always invoke `generate_cli.py`. NEVER modify the script.

---

## Invocation

### Setup (once)
```bash
pip install -r requirements.txt
```
Add to `.env` at the repo root:
```
CIRRASCALE_API_KEY=<your-cirrascale-bearer-token>
```

### Command
```bash
python generate_cli.py "<prompt 1>" "<prompt 2>" "<prompt 3>" [options]
```

Prompt 1 is the SD2.1 one — keep it short and concrete. Prompts 2 and 3 are the
Cirrascale ones — expand them fully. Fewer prompts than images? The last one is
reused for the rest.

### Example
```bash
python generate_cli.py \
    "birthday table with blue balloons and a yellow backdrop" \
    "a fairy-light-draped balloon arch, warm golden hour, shallow depth of field" \
    "pastel dessert tablescape, soft window light, cobalt and lemon palette" \
    --size 512x512 --steps 1 --guidance-scale 6.5 --json
```

### Arguments

| Argument         | Flag                  | Required | Default                  | Description                                    |
|------------------|-----------------------|----------|---------------------------|-------------------------------------------------|
| Prompts          | *(positional)*        | ✅ Yes   | —                         | One quoted prompt per image (prompt 1 → SD2.1) |
| Model            | `--model`             | ❌ No    | `stabilityai/sdxl-turbo`  | Cirrascale model identifier (cloud images only) |
| Size             | `--size`              | ❌ No    | `512x512`                 | `WIDTHxHEIGHT`                                 |
| Count            | `--n`                 | ❌ No    | `3`                       | Images in the set — clamped to 3, never 4      |
| Cloud only       | `--no-local`          | ❌ No    | off                       | Skip the SD2.1 image; run all on Cirrascale    |
| Local steps      | `--local-steps`       | ❌ No    | `20`                      | SD2.1 denoising steps (`20` or `50`)           |
| Negative prompt  | `--negative-prompt`   | ❌ No    | *(none)*                  | SD2.1 negative prompt                          |
| Local timeout    | `--local-timeout`     | ❌ No    | `900`                     | Seconds allowed for the SD2.1 image            |
| Inference steps  | `--steps`             | ❌ No    | `1`                       | `num_inference_steps`                          |
| Guidance scale   | `--guidance-scale`    | ❌ No    | `6.5`                     | Prompt adherence strength                      |
| Seed             | `--seed`              | ❌ No    | *(none)*                  | Fixed seed for reproducibility                 |
| Seed increment   | `--seed-increment`    | ❌ No    | *(none)*                  | Per-image seed step when `n > 1`               |
| Response format  | `--response-format`   | ❌ No    | `b64_json`                | `b64_json` (saved to disk) or `url`            |
| Output dir       | `--output-dir`        | ❌ No    | `Skills/image-generation/output` (workspace-relative) | Where decoded images are saved |
| Filename prefix  | `--prefix`            | ❌ No    | `cirrascale`              | Prefix for saved filenames                     |
| Timeout          | `--timeout`           | ❌ No    | `90`                      | Request timeout in seconds                      |
| JSON output      | `--json`              | ❌ No    | off                       | Emit a JSON summary instead of text             |

Note: the script always calls the API with `stream=true` internally — Cirrascale's
non-streaming mode was confirmed to hang 90s+ with no response, so there is no
`--stream` flag to turn off.

---

## Trigger Conditions

Invoke this skill when the user:

- Asks for decor ideas, theme ideas, setup ideas, or a look/concept for an event
  — even when they never say the word "image". "I want a decor idea for a
  birthday with blue balloons and a yellow backdrop" is an image request.
- Asks to generate an image via the cloud, an API, or names "Cirrascale" specifically
- Asks for image generation
- Names a Cirrascale-hosted model (e.g. `stabilityai/sdxl-turbo`) directly
- Asks for several images/ideas/concepts — the set is 3: 1 local SD2.1 on the
  NPU + 2 Cirrascale SDXL, produced by one call (see "Hybrid setup")
- Asks for local/NPU/SD2.1 generation explicitly — that is image 1 of any run;
  `--n 1` gives just the local one

A decor idea answered as text only is an incomplete answer. Write the text
description *and* run the CLI to produce the image for it. Pass one prompt per
distinct concept in a single call.

### ⚠️ One call, never parallel

Pass all prompts to a single `generate_cli.py` invocation — it sequences the
cloud requests internally. Never launch several `generate_cli.py` calls in
parallel.

Cirrascale silently drops concurrent requests: the extra ones come back as
HTTP 200 with `content-type: text/event-stream` and an *empty body* — no
`data:` event — so `_call_streaming` returns nothing and the CLI exits 2 with
`ERROR: no response payload from Cirrascale API`. Measured on this deployment:

| Mode          | Outcome                          |
|---------------|----------------------------------|
| 3 in parallel | 1 image, 2 errors                |
| 2 in parallel | 2 images, but 45s and 56s each   |
| 3 sequential  | 3 images, ~24s each              |

Budget ~24s per image, so three concepts take ~75s. That is normal, not a hang.

If you asked for N concepts (N ≤ 3), you must end up with N entries in
`saved_paths`. Fewer means a backend failed — the `errors` array names which
one. Say so and re-run the missing one; never deliver N prompts with fewer
than N images, and never silently regenerate the NPU image on Cirrascale.

---

## Hybrid setup (local NPU + cloud)

Every set splits across both backends, and `generate_cli.py` does the split
itself — one call, no hand-sequencing:

| Image | Backend | Model | Prompt style |
|-------|---------|-------|--------------|
| 1 | **Local NPU** (Snapdragon, ORT-QNN) | Stable Diffusion 2.1 | **Simple** — one short decor idea, ~10–15 words |
| 2 | Cirrascale cloud | `stabilityai/sdxl-turbo` | Full detailed prompt (style, lighting, palette, composition) |
| 3 | Cirrascale cloud | `stabilityai/sdxl-turbo` | Full detailed prompt |

The set is **always three** — there is no four-image flow. A request for four
ideas is answered with three images (`--n` is capped and extra prompts are
dropped, with a NOTE on stderr). Use `--n 1`/`--n 2` for a smaller set —
image 1 still goes to the NPU unless `--no-local` is passed.

### Prompt rules per backend

- **SD2.1 (local NPU) — keep the prompt simple.** SD2.1 at 20 steps on the NPU
  handles a plain, concrete decor idea far better than a dense one. Write one
  short decor concept: subject, colors, setting. No multi-clause camera/lighting
  stacking, no long modifier chains.
  - ✅ `"birthday table with blue balloons and a yellow backdrop"`
  - ❌ `"ultra-detailed cinematic 8k birthday tablescape, volumetric golden-hour
    rim lighting, shallow depth of field, pastel cobalt balloon cluster..."`
- **Cirrascale SDXL — expand fully**, exactly as the rest of this file describes.
  The two cloud concepts must be visually distinct from each other *and* from
  the SD2.1 one.

### The session server is handled for you

`generate_cli.py` checks `127.0.0.1:50002` and starts
`Stable-Diffusion-2-1/session_server.py` (in its own venv) if nothing is
listening, then waits up to `--local-server-timeout` seconds for the ORT-QNN
sessions to load. First load takes a while; that is normal, not a hang. Leaving
the server running between runs makes later calls much faster.

### The call

```bash
python ".\Skills\image-generation\generate_cli.py" ^
  "<simple SD2.1 decor idea>" "<detailed prompt 2>" "<detailed prompt 3>" ^
  --prefix concept --json
```

The two Cirrascale requests are issued strictly one after another inside the
script — Cirrascale drops concurrent requests. Budget ~24s per cloud image; the
local NPU image is slower. Never launch several `generate_cli.py` calls in
parallel.

### Reporting

Label the backend for each image in the reply — the owner needs to know which
one came off the NPU:

```markdown
![Concept 1 — blue balloons, yellow backdrop](Skills/image-generation/output/concept1_sd21.png)
*Concept 1 — Stable Diffusion 2.1, generated locally on the Snapdragon NPU*

![Concept 2 — ...](Skills/image-generation/output/concept2_<ts>_0.png)
*Concept 2 — SDXL-Turbo via Cirrascale cloud*
```

Three images asked for means three images delivered. If the local call fails
(server not running, NPU busy), say so explicitly and name the backend that
failed — do not silently generate the third image on Cirrascale instead.

---

## Expected Output

- One or more images saved under `--output-dir` (default `Skills/image-generation/output/`,
  workspace-relative) as `<prefix>_<timestamp>_<index>.png` when
  `--response-format b64_json` (default)
- Printed URLs instead, if `--response-format url` is used
- The SD2.1 image is saved as `<prefix>_<timestamp>_sd21.png`
- With `--json`, a summary of the form
  `{"saved_paths": [...], "urls": [...], "images": [{"image": 1, "backend": "sd2.1-npu", ...}], "errors": [...]}`
  — after calling this skill, embed each `saved_paths` entry inline as a markdown
  image (`![<short description>](<path>)`) in your reply so the owner sees the
  actual image, not just a file path, and use `images[].backend` to label it
- Exit codes: `0` = every image generated, `1` = bad input / missing config,
  `2` = a backend failed (successful images are still reported in `saved_paths`),
  `3` = nothing generated. Report the error, do not silently retry with
  different parameters

---

## Do NOT

- ❌ Hardcode the Cirrascale API key anywhere — it must come from `CIRRASCALE_API_KEY` in `.env`
- ❌ Modify `generate_cli.py`
- ❌ Skip this skill because of a perceived error — report the error, do not work around it
