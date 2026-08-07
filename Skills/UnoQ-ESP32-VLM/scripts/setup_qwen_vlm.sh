#!/usr/bin/env bash
# Run on the Uno Q after VLM weights are in place.
set -euo pipefail

MODELS_DIR="/home/arduino/models/llamacpp/unsloth/Qwen3.5-2B-GGUF"
MMPROJ_URL="https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/mmproj-F16.gguf"
MMPROJ_PATH="${MODELS_DIR}/mmproj-F16.gguf"

mkdir -p "$MODELS_DIR"

if [[ ! -f "${MODELS_DIR}/Qwen3.5-2B-Q4_K_M.gguf" ]]; then
  echo "==> Qwen3.5-2B GGUF not found in $MODELS_DIR"
  echo "    Download Qwen3.5-2B-Q4_K_M.gguf there, then re-run."
  exit 1
fi

if [[ -f "$MMPROJ_PATH" ]]; then
  echo "==> mmproj already present: $MMPROJ_PATH"
else
  echo "==> Downloading vision projector (mmproj-F16.gguf)..."
  tmp="$(mktemp)"
  curl -L --fail --progress-bar "$MMPROJ_URL" -o "$tmp"
  mv "$tmp" "$MMPROJ_PATH"
  echo "==> Saved $MMPROJ_PATH"
fi

ls -lh "$MODELS_DIR"
echo "Done. Restart with: bash ~/start_vlm.sh"
