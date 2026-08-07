#!/usr/bin/env bash
# Wipe Uno Q VLM install, push scripts + Qwen3.5-2B, start service.
#
# Usage (from repo root, Uno Q on USB):
#   ./scripts/deploy_to_unoq.sh
#   ./scripts/deploy_to_unoq.sh --keep-model   # reuse weights already on the board
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
LOCAL_MODEL_DIR="$ROOT/models/Qwen3.5-2B-GGUF"
BOARD_MODEL_DIR="/home/arduino/models/llamacpp/unsloth/Qwen3.5-2B-GGUF"
BOARD_LLAMACPP_LINK="/var/lib/arduino-app-cli/models/llamacpp"
HF="https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main"
MODEL_FILE="Qwen3.5-2B-Q4_K_M.gguf"
MMPROJ_FILE="mmproj-F16.gguf"
KEEP_MODEL=0

for arg in "$@"; do
  case "$arg" in
    --keep-model) KEEP_MODEL=1 ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

need_adb() {
  command -v adb >/dev/null || {
    echo "adb not found. Install: brew install android-platform-tools" >&2
    exit 1
  }
  if ! adb devices | awk 'NR>1 && /device$/{ok=1} END{exit ok?0:1}'; then
    echo "No Uno Q on USB. Check: adb devices" >&2
    exit 1
  fi
}

download_local_models() {
  mkdir -p "$LOCAL_MODEL_DIR"
  for f in "$MODEL_FILE" "$MMPROJ_FILE"; do
    if [[ -f "$LOCAL_MODEL_DIR/$f" ]]; then
      echo "==> Have local $f"
      continue
    fi
    # Prefer pulling from board if it still has the file (faster than HF).
    if adb shell "test -f '$BOARD_MODEL_DIR/$f' && echo OK" | grep -q OK; then
      echo "==> Pulling $f from Uno Q → Mac cache…"
      adb pull "$BOARD_MODEL_DIR/$f" "$LOCAL_MODEL_DIR/$f"
      continue
    fi
    echo "==> Downloading $f to Mac cache…"
    curl -L --fail --progress-bar -o "$LOCAL_MODEL_DIR/$f.partial" "$HF/$f"
    mv "$LOCAL_MODEL_DIR/$f.partial" "$LOCAL_MODEL_DIR/$f"
  done
  ls -lh "$LOCAL_MODEL_DIR"
}

wipe_board() {
  echo "==> Stopping / destroying old apps…"
  adb shell 'arduino-app-cli app stop user:vlm 2>/dev/null || true'
  adb shell 'arduino-app-cli app destroy user:vlm 2>/dev/null || true'
  adb shell 'arduino-app-cli app stop user:balloon_counter 2>/dev/null || true'
  adb shell 'arduino-app-cli app destroy user:balloon_counter 2>/dev/null || true'
  adb shell 'arduino-app-cli app stop user:esp32_cam_bridge 2>/dev/null || true'
  adb shell 'arduino-app-cli app destroy user:esp32_cam_bridge 2>/dev/null || true'

  echo "==> Removing old scripts / app dirs / frames…"
  adb shell 'rm -rf \
    ~/ArduinoApps/vlm \
    ~/ArduinoApps/balloon_counter \
    ~/ArduinoApps/esp32_cam_bridge \
    ~/vlm \
    ~/capture_esp32_ap.sh \
    ~/start_vlm.sh \
    ~/start_balloon_counter.sh \
    ~/setup_qwen_vlm.sh'

  if [[ "$KEEP_MODEL" -eq 0 ]]; then
    echo "==> Removing old model weights on board…"
    adb shell 'rm -rf ~/models/llamacpp'
    adb shell "rm -rf '$BOARD_LLAMACPP_LINK' 2>/dev/null || true"
  else
    echo "==> Keeping existing model weights (--keep-model)"
  fi
}

install_vlm_app() {
  echo "==> Creating vlm app…"
  adb shell 'arduino-app-cli app new vlm --no-sketch -i "📷" -d "Local Qwen VLM" -b arduino:llm'
  adb shell "cat > /home/arduino/ArduinoApps/vlm/app.yaml << 'EOF'
name: vlm
icon: \"📷\"
description: Local Qwen VLM service
bricks:
  - arduino:llm
EOF"
  adb shell "cat > /home/arduino/ArduinoApps/vlm/python/main.py << 'EOF'
from arduino.app_bricks.llm import LargeLanguageModel
from arduino.app_utils import App
import time

llm = LargeLanguageModel(model=\"llamacpp:Qwen3.5-2B-Q4_K_M\")

def loop():
    time.sleep(60)

App.run(user_loop=loop)
EOF"
  adb shell 'mkdir -p ~/vlm'
}

push_scripts() {
  echo "==> Pushing scripts…"
  adb push "$SCRIPTS/capture_esp32_ap.sh" /home/arduino/capture_esp32_ap.sh
  adb push "$SCRIPTS/start_vlm.sh" /home/arduino/start_vlm.sh
  adb push "$SCRIPTS/setup_qwen_vlm.sh" /home/arduino/setup_qwen_vlm.sh
  adb shell 'chmod +x ~/capture_esp32_ap.sh ~/start_vlm.sh ~/setup_qwen_vlm.sh'
}

push_models() {
  echo "==> Ensuring model dir + llama.cpp symlink…"
  adb shell "mkdir -p '$BOARD_MODEL_DIR' /home/arduino/models/llamacpp"
  adb shell "
    if [ ! -L '$BOARD_LLAMACPP_LINK' ] && [ ! -d '$BOARD_LLAMACPP_LINK' ]; then
      ln -s /home/arduino/models/llamacpp '$BOARD_LLAMACPP_LINK'
    elif [ -d '$BOARD_LLAMACPP_LINK' ] && [ ! -L '$BOARD_LLAMACPP_LINK' ]; then
      # Prefer home storage (root disk is tight)
      rm -rf '$BOARD_LLAMACPP_LINK'
      ln -s /home/arduino/models/llamacpp '$BOARD_LLAMACPP_LINK'
    fi
  "

  if [[ "$KEEP_MODEL" -eq 1 ]]; then
    if adb shell "test -f '$BOARD_MODEL_DIR/$MODEL_FILE' && test -f '$BOARD_MODEL_DIR/$MMPROJ_FILE' && echo OK" | grep -q OK; then
      echo "==> Board already has model files"
    else
      echo "==> --keep-model set but files missing; will push/download" >&2
      KEEP_MODEL=0
    fi
  fi

  if [[ "$KEEP_MODEL" -eq 0 ]]; then
    download_local_models
    echo "==> Pushing model weights (slow, ~1.9 GB)…"
    adb push "$LOCAL_MODEL_DIR/$MODEL_FILE" "$BOARD_MODEL_DIR/$MODEL_FILE"
    adb push "$LOCAL_MODEL_DIR/$MMPROJ_FILE" "$BOARD_MODEL_DIR/$MMPROJ_FILE"
  fi

  adb shell "cat > /home/arduino/models/llamacpp/models.ini << EOF
[Qwen3.5-2B-Q4_K_M]
model = /models/unsloth/Qwen3.5-2B-GGUF/Qwen3.5-2B-Q4_K_M.gguf
mmproj = /models/unsloth/Qwen3.5-2B-GGUF/mmproj-F16.gguf
EOF"
  adb shell "ls -lh '$BOARD_MODEL_DIR'"
}

start_vlm() {
  echo "==> Starting VLM…"
  adb shell 'bash ~/start_vlm.sh'
  sleep 5
  adb shell 'curl -s http://127.0.0.1:9999/v1/models' | head -c 300 || true
  echo
}

need_adb
# Cache model on Mac before wipe (pull from board or Hugging Face)
if [[ "$KEEP_MODEL" -eq 0 ]]; then
  download_local_models
fi
wipe_board
install_vlm_app
push_scripts
push_models
start_vlm

echo
echo "Deploy complete."
echo "  Ask:  ./scripts/ask_vlm.py \"What do you see?\""
echo "  Or:   ./scripts/ask_vlm.py --save -- \"How many balloons?\""
