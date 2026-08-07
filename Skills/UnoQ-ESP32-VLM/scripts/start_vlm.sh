#!/usr/bin/env bash
# Start the onboard VLM (llama.cpp) with enough RAM for Qwen3.5-2B.
set -euo pipefail

APP="${VLM_APP:-user:vlm}"

arduino-app-cli app start "$APP"

for i in $(seq 1 30); do
  name="$(docker ps --format '{{.Names}}' | grep 'vlm-llamacpp-models-runner-1' | head -1 || true)"
  if [[ -n "$name" ]]; then
    docker update --memory 3g --memory-swap 5g "$name" || true
    break
  fi
  # Fallback while migrating off old app name
  name="$(docker ps --format '{{.Names}}' | grep 'llamacpp-models-runner-1' | head -1 || true)"
  if [[ -n "$name" ]]; then
    docker update --memory 3g --memory-swap 5g "$name" || true
    break
  fi
  sleep 1
done

echo "VLM running (llama on :9999)."
