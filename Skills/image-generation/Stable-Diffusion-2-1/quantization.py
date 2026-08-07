"""Quantization helpers for the precompiled SD2.1 QNN ONNX package.

Unlike the SD3 ORT QNN export (whose wrapper .onnx files insert QuantizeLinear/
DequantizeLinear nodes so callers see plain float32), this package's
text_encoder.onnx / unet.onnx / vae.onnx declare their *outer* graph I/O as raw
uint16 (confirmed via onnx.load and a live sess.run() call). Callers must
quantize inputs and dequantize outputs manually using the affine scale/zero_point
pairs published in metadata.json, following the standard ONNX QuantizeLinear/
DequantizeLinear formula.
"""

import json
import os

import numpy as np

# The model package is multi-GB and lives outside the repo, so its location is
# machine-specific: setup.ps1 writes SD21_MODEL_DIR into Skills/.env and
# start.ps1 exports it. The sibling Model_Bins folder is the fallback.
_DEFAULT_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Model_Bins")
MODEL_DIR = os.environ.get("SD21_MODEL_DIR") or _DEFAULT_MODEL_DIR

_METADATA_PATH = os.path.join(MODEL_DIR, "metadata.json")
if not os.path.isfile(_METADATA_PATH):
    raise SystemExit(
        f"SD2.1 model package not found: {_METADATA_PATH}\n"
        "Set SD21_MODEL_DIR to the folder holding text_encoder.onnx / unet.onnx / "
        "vae.onnx, their *_qairt_context.bin siblings and metadata.json, or re-run "
        "setup.ps1 with -ModelBins <path>."
    )

with open(_METADATA_PATH) as f:
    _METADATA = json.load(f)


def _qparams(onnx_file, tensor_name, kind):
    q = _METADATA["model_files"][onnx_file][kind][tensor_name]["quantization_parameters"]
    return q["scale"], q["zero_point"]


def quantize(x, scale, zero_point, dtype=np.uint16):
    return np.clip(np.round(np.asarray(x, dtype=np.float64) / scale) + zero_point, 0, 65535).astype(dtype)


def dequantize(x, scale, zero_point):
    return (x.astype(np.float32) - zero_point) * np.float32(scale)


TEXT_EMBEDDING_SCALE, TEXT_EMBEDDING_ZP = _qparams("text_encoder.onnx", "text_embedding", "outputs")

UNET_LATENT_SCALE, UNET_LATENT_ZP = _qparams("unet.onnx", "latent", "inputs")
UNET_TIMESTEP_SCALE, UNET_TIMESTEP_ZP = _qparams("unet.onnx", "timestep", "inputs")
UNET_TEXT_EMB_SCALE, UNET_TEXT_EMB_ZP = _qparams("unet.onnx", "text_emb", "inputs")
UNET_OUTPUT_LATENT_SCALE, UNET_OUTPUT_LATENT_ZP = _qparams("unet.onnx", "output_latent", "outputs")

VAE_LATENT_SCALE, VAE_LATENT_ZP = _qparams("vae.onnx", "latent", "inputs")
VAE_IMAGE_SCALE, VAE_IMAGE_ZP = _qparams("vae.onnx", "image", "outputs")
