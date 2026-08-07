# Stable Diffusion 2.1 — ONNX Runtime QNN Pipeline

Runs the precompiled, quantized (w8a16) Stable Diffusion 2.1 QNN ONNX package
at
`C:\hackathon_code\compute_sd\ORT\stable_diffusion_v2_1-precompiled_qnn_onnx-w8a16-qualcomm_snapdragon_x_elite`
end to end via ONNX Runtime's QNN execution provider, to generate an image
from a text prompt on the Snapdragon X Elite NPU.

## Hardware requirement

The context binaries were compiled for **HTP v73 (Snapdragon X Elite)**, per
the model package's `metadata.json` (`htp_version: 73`, `soc_model: 60`).

## Critical note: QNN SDK version — requires onnxruntime-qnn 2.4.0

The context binaries were compiled with **QAIRT 2.45.0**. The `QnnHtp.dll`
bundled with `onnxruntime-qnn==1.24.1` (used by the sibling `sd3_ort_qnn`
project) is QAIRT 2.42 — **too old** to load them, failing with:

```
<E> Using newer context binary on old SDK
<E> Fail to get context blob with err 5000
<E> Failed to create context from binary with err 0x1388
```

This pipeline therefore targets **`onnxruntime-qnn==2.4.0`**, which bundles
QAIRT **2.48.40** and loads these binaries.

### 2.x is a plugin EP — what that changes in the code

`onnxruntime-qnn` 2.x is no longer a drop-in replacement for the `onnxruntime`
wheel; it is a **plugin execution provider**. Three consequences, all handled
in `session_server.py`:

1. **Core runtime is a separate dependency.** The wheel installs an
   `onnxruntime_qnn` package and depends on the stock `onnxruntime` wheel
   (`>= 1.24.1`; 2.4.0 was compiled against 1.26.0, which is what
   `requirements.txt` pins). `onnxruntime.get_available_providers()` no longer
   lists `QNNExecutionProvider`.
2. **The EP library must be registered before use, and providers are set on
   `SessionOptions`** rather than passed to `InferenceSession`:
   ```python
   import onnxruntime as ort
   import onnxruntime_qnn as qnn_ep

   ort.register_execution_provider_library(qnn_ep.get_ep_name(), qnn_ep.get_library_path())
   npu = [d for d in ort.get_ep_devices()
          if d.ep_name == qnn_ep.get_ep_name() and d.device.type == ort.OrtHardwareDeviceType.NPU]

   so = ort.SessionOptions()
   so.add_provider_for_devices(npu, {"backend_path": qnn_ep.get_qnn_htp_path(), ...})
   sess = ort.InferenceSession(path, sess_options=so)   # no providers= argument
   ```
   The plugin advertises an `OrtEpDevice` per QNN backend it can reach (NPU,
   GPU *and* CPU), so the NPU filter matters — only the HTP/NPU device can run
   these context binaries.
3. **No more `qnn_libs/` staging.** The QAIRT DLLs ship inside the installed
   package and `qnn_ep.get_qnn_htp_path()` locates them, so the previously
   hand-staged `qnn_libs/` directory (and the `PATH` manipulation that went
   with it) is gone.

## Setup

1. Create a Python 3.12 venv and install `requirements.txt`:
   ```
   py -3.12 -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```
   `torch` has no win_arm64 wheel on PyPI, which is why `requirements.txt`
   points `pip` at PyTorch's own CPU wheel index via `--extra-index-url`
   (torch here is only used for CPU-side tensor math -- the actual model
   inference runs on the NPU through `onnxruntime-qnn`).

   If you are upgrading an existing venv from the 1.x layout, uninstall it
   first — the 1.x wheel owns the `onnxruntime` module files and would collide
   with the standalone core wheel:
   ```
   venv\Scripts\pip uninstall -y onnxruntime-qnn
   venv\Scripts\pip install -r requirements.txt
   ```
2. Either a **native win_arm64** or a **win_amd64** Python works on Snapdragon,
   because 2.4.0 ships QAIRT DLLs for both: the win_arm64 wheel puts native
   arm64 DLLs flat in `onnxruntime_qnn/`, and the win_amd64 wheel ships
   `onnxruntime_qnn/libs/{amd64,arm64ec}/` and selects `arm64ec` (ARM64 code
   with the x64 ABI, so the NPU is still reachable) when it detects an x64
   process on an ARM64 host. `import onnxruntime_qnn` does that selection and
   the DLL-directory setup itself — nothing to configure. Verify with:
   ```
   venv\Scripts\python.exe -c "import onnxruntime_qnn as q; print(q.get_qnn_htp_path())"
   ```
   A mismatch here fails loudly at session-server startup (no NPU
   `OrtEpDevice`, or a DLL load error) rather than silently producing bad
   images.

## Usage

Start the persistent session server (loads all three ORT QNN sessions once,
keeps them warm):

```
venv\Scripts\python.exe session_server.py
```

In a second terminal, generate an image:

```
venv\Scripts\python.exe generate.py --prompt "a photo of an astronaut riding a horse" --steps 20 --output output.png
```

Options:
- `--prompt` (required): text prompt
- `--negative-prompt`: unconditional prompt (default: empty string)
- `--seed`: integer seed for the initial latent (default: 42)
- `--steps`: 20 or 50 (default: 20)
- `--guidance-scale`: float in `[5.0, 15.0]` (default: 7.5)
- `--output`: output PNG path (default: `output.png`)

## Pipeline structure

- `server_config.py` — shared host/port/authkey constants for the
  `multiprocessing.managers` connection between `session_server.py` and
  `qnn_runner.py`, so the two can't drift out of sync.
- `quantization.py` — reads `metadata.json` from the model package once;
  exposes `quantize()`/`dequantize()` helpers and the per-tensor scale/
  zero_point constants for every model I/O.
- `tokenizer.py` — CLIP tokenizer (`openai/clip-vit-large-patch14`), 77-token
  int32 output. Ported verbatim from the sibling `qnn_context_binary` SD2.1
  package (backend-independent).
- `scheduler.py` — DPM-Solver++ multistep scheduler (v-prediction) and
  classifier-free guidance merge. Ported verbatim from the same sibling.
- `session_server.py` — persistent process that pre-loads the three ORT QNN
  sessions (`text_encoder`, `unet`, `vae`) and serves inference requests over
  `multiprocessing.managers` on port 50002 (a different port from the SD3
  pipeline's server, so both can run at once).
- `qnn_runner.py` — client wrapper: connects to `session_server.py` and
  exposes clean `run_text_encoder()` / `run_unet()` / `run_vae()` functions
  that handle quantizing inputs and dequantizing outputs internally.
- `generate.py` — CLI entrypoint: tokenize → text encode → denoising loop
  (UNet + scheduler) → VAE decode → save PNG.

### Why manual quantization is needed here (and not in the SD3 pipeline)

The SD3 ORT QNN export's wrapper `.onnx` files embed `QuantizeLinear`/
`DequantizeLinear` nodes around the `EPContext` op so the *outer* graph
presents plain float32 tensors to ONNX Runtime callers — quantization is
transparent. This SD2.1 package's wrapper `.onnx` files declare their outer
graph I/O as literal `uint16` (confirmed by loading them and inspecting
`sess.get_inputs()/get_outputs()` — `dtype=tensor(uint16)` throughout, except
for `text_encoder.onnx`'s `tokens` input which stays `int32`), so this
pipeline's `quantization.py`/`qnn_runner.py` do that affine (de)quantization
in Python using the `scale`/`zero_point` pairs from `metadata.json`.

No external VAE scaling factor (e.g. the standard SD2.1 `0.18215`) or UNet
latent rescale is applied — it's baked into the quantization parameters
already (confirmed by the `image` output's scale math: `scale=1.526e-5`,
`zero_point=0` puts dequantized values in `[0, ~1]`, matching a direct
`* 255` → `uint8` conversion with no separate unscale step).
