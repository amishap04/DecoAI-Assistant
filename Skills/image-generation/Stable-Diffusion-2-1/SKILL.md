# SKILL.md — sd2.1-image-generation

## Overview

| Field       | Details                                                                 |
|-------------|-------------------------------------------------------------------------|
| **Name**    | sd2.1-image-generation                                                  |
| **Type**    | Text-to-Image Generation (Local NPU only)                               |
| **Script**  | `generate.py`                                                           |
| **Hardware**| Qualcomm Snapdragon NPU via ONNX Runtime QNN execution provider         |

**Description:**
Local-only image generation skill for Stable Diffusion 2.1, accelerated on a Qualcomm
Snapdragon NPU. Pass a prompt to `generate.py` — it runs the full text-encode → denoise →
VAE decode pipeline against the persistent `session_server.py` process and saves the result.

---

## ⚠️ CRITICAL

> `session_server.py` MUST be running before `generate.py` is invoked — it holds the
> pre-loaded ORT-QNN sessions that `generate.py` connects to.
> Always invoke `generate.py` for generation. NEVER call any other script directly.
> NEVER modify any script.
> This pipeline requires `onnxruntime-qnn==2.4.0` (plugin EP, bundles QAIRT SDK 2.48) —
> older SDK DLLs cannot load this model's context binaries.

---

## Invocation

### Step 0 — start the session server (once, kept running)
```bash
python "C:\Users\qc_de\.openclaw\workspace\Skills\image-generation\Stable-Diffusion-2-1\session_server.py"
# Optional: specify a custom model directory
python "C:\Users\qc_de\.openclaw\workspace\Skills\image-generation\Stable-Diffusion-2-1\session_server.py"  --model_dir "C:\Users\qc_de\.openclaw\workspace\Skills\image-generation\Stable-Diffusion-2-1\Model_Bins"
```
Leave this process running in the background. It listens on `127.0.0.1:50002`.

### Command
```bash
python "C:\Users\qc_de\.openclaw\workspace\Skills\image-generation\Stable-Diffusion-2-1\generate.py" --prompt "prompt text" [--negative-prompt "negative prompt text"] [--seed 4] [--steps 50] [--guidance-scale 7.5] [--output "output.png"]
```

### Example
```bash
python "C:\Users\qc_de\.openclaw\workspace\Skills\image-generation\Stable-Diffusion-2-1\generate.py" --prompt "Taipei night market neon hero shot"
```

### Arguments

| Argument         | Flag                | Required | Description                                                        |
|------------------|----------------------|----------|----------------------------------------------------------------------|
| Prompt           | `--prompt`           | ✅ Yes   | Image prompt as a quoted string                                     |
| Negative prompt  | `--negative-prompt`  | ❌ No    | Unconditional/negative prompt (default: empty string)                |
| Seed             | `--seed`             | ❌ No    | Integer seed for the initial latent (default: `42`)                  |
| Steps            | `--steps`            | ❌ No    | Number of denoising steps, `20` or `50` (default: `20`)              |
| Guidance scale   | `--guidance-scale`   | ❌ No    | Classifier-free guidance scale, in `[5.0, 15.0]` (default: `7.5`)     |
| Output path      | `--output`           | ❌ No    | Path to save the generated PNG (default: `output.png` in the cwd)    |

---

## Trigger Conditions

Invoke this skill when the user:

- Says "generate an image", "create a picture", "draw", "make an image of..." AND local/NPU
  generation is specifically requested (e.g. "use the NPU", "generate locally", "use SD2.1")
- Provides a descriptive text prompt intended to produce a visual output via the local SD2.1
  pipeline specifically
- Uses words like "visualize", "illustrate", "render", "show me" in the context of the local
  SD2.1 pipeline

---

## Hybrid runs — SD2.1 (NPU) alongside SDXL (Cirrascale)

A **3-image request splits across both backends**: image 1 here on the local NPU,
images 2 and 3 on Cirrascale SDXL via `../generate_cli.py`. Routing, prompt-style
rules, and reporting are owned by `Skills/image-generation/SKILL.md` → "Hybrid
setup"; this section covers only how to run *this* pipeline as part of it.

### Run from the workspace root

Use the pipeline's **own venv interpreter** — the QNN packages are not in the
system Python — and save into the shared output directory so the path can be
embedded like any cloud result:

```bash
# 0. server up? (start it only if nothing is listening)
netstat -ano | findstr :50002

Skills\image-generation\Stable-Diffusion-2-1\venv\Scripts\python.exe ^
  Skills\image-generation\Stable-Diffusion-2-1\session_server.py

# 1. local image (concept 1) — simple prompt
Skills\image-generation\Stable-Diffusion-2-1\venv\Scripts\python.exe ^
  Skills\image-generation\Stable-Diffusion-2-1\generate.py ^
  --prompt "<simple decor idea, ~10-15 words>" ^
  --steps 20 --guidance-scale 7.5 --seed 42 ^
  --output "Skills\image-generation\output\concept1_sd21.png"

# 2-3. cloud images — strictly one at a time
python ".\Skills\image-generation\generate_cli.py" "<detailed prompt 2>" --prefix concept2 --json
python ".\Skills\image-generation\generate_cli.py" "<detailed prompt 3>" --prefix concept3 --json
```

### Ordering and timing

- The two Cirrascale calls must stay **sequential** — that limit is cloud-side only.
- This local call may run **alongside** them; it never touches the API. Starting the
  session server first and firing the cloud calls while sessions load hides most of
  the cold-load wait.
- Budget ~24s per cloud image; the local one is slower (session load can take minutes
  on a cold start, then ~1-2 min for 20 steps).

### Prompt style here vs. cloud

Keep the SD2.1 prompt **simple** — one short, concrete decor concept (subject,
colors, setting). SD2.1 at 20 steps on the NPU degrades on dense prompts, while the
SDXL concepts take the full detailed treatment.

- ✅ `"wedding reception hall with a white floral arch and fairy lights"`
- ❌ `"ultra-detailed cinematic 8k reception hall, volumetric golden-hour rim
  lighting, shallow depth of field, cascading peony installation..."`

### Sizes and failure reporting

- Local output is fixed at **512×512** (cloud default is also 512×512; non-default
  `--size`/`--steps` on sdxl-turbo have returned HTTP 500 on this deployment).
- If this local call fails (server down, NPU busy), say so and name the backend that
  failed — never silently generate the third image on Cirrascale instead.

---

## Expected Output

- One image saved to the path given by `--output` (default: `output.png` in the working
  directory `generate.py` was run from)
- Console prints per-step denoising progress (timestep, tensor stats) and a final "Saved
  image to ..." confirmation

---

## Do NOT

- ❌ Call `generate.py` before `session_server.py` is running
- ❌ Call any script other than `generate.py` for generation
- ❌ Use any cloud image generation tool or API directly
- ❌ Modify any script
- ❌ Skip this skill because of a perceived error — report the error, do not work around it
