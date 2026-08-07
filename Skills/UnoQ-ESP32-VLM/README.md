# Uno Q + ESP32-CAM VLM

Mac-side scripts to capture from ESP32-CAM and ask **Qwen** on the Arduino Uno Q.

## Prerequisites

- Uno Q on USB (`adb devices` shows it)
- VLM service running on the board (`adb shell 'bash ~/start_vlm.sh'`)
- ESP32-CAM soft-AP named `ESP32-CAM-MB` (for capture)

## Wiring (Uno Q ↔ ESP32-CAM)

| Uno Q | ESP32-CAM | Notes |
|-------|-----------|--------|
| **RX** | **U0T** (TX) | Cross-wired: Uno Q receives from ESP32 TX |
| **TX** | **U0R** (RX) | Cross-wired: Uno Q transmits to ESP32 RX |
| **5V** | **5V** | Power |
| **GND** | **GND** | Use the GND pad **beside 5V** on the ESP32-CAM |

```
Uno Q          ESP32-CAM
─────          ─────────
 RX  ◄────────  U0T (TX)
 TX  ────────►  U0R (RX)
 5V  ──────────  5V
 GND ──────────  GND (next to 5V)
```

## Scripts

### `deploy_to_unoq.sh` — wipe board + push scripts + Qwen model

```bash
cd UnoQ-ESP32-VLM
./scripts/deploy_to_unoq.sh

# Faster re-deploy when weights are already on the board:
./scripts/deploy_to_unoq.sh --keep-model
```

Deletes old VLM apps/scripts on the Uno Q, pushes these scripts, installs
**Qwen3.5-2B-Q4_K_M** + `mmproj-F16` (cached under `models/` on your Mac), and starts the service.

### `ask_vlm.py` — capture / image + ask (main entry)

```bash
cd UnoQ-ESP32-VLM

# Capture from ESP32, then ask
./scripts/ask_vlm.py "How many balloons are in this photo?"

# Also save the frame to your Mac
./scripts/ask_vlm.py --save -- "How many balloons are in this photo?"
./scripts/ask_vlm.py --save my_shot.jpg "How many balloons?"

# Use a local image
./scripts/ask_vlm.py --image photo.jpg "What do you see?"

# Reuse latest frame already on the Uno Q
./scripts/ask_vlm.py --latest "Describe this image."
```

Prints the model reply to stdout. Vision calls can take 1–3+ minutes on Qwen3.5-2B.

### `capture_esp32_ap.sh` — board helper (used by `ask_vlm.py`)

Hops the Uno Q onto `ESP32-CAM-MB`, saves a JPEG, restores HaQathon.

```bash
adb shell 'bash ~/capture_esp32_ap.sh'
```

### `start_vlm.sh` — start Qwen on the board (enough RAM for 2B)

```bash
adb shell 'bash ~/start_vlm.sh'
```

### `setup_qwen_vlm.sh` — board-side mmproj helper

Usually covered by `deploy_to_unoq.sh`; keep for manual repairs on the board.

## Board paths (reference)

| What | Path |
|------|------|
| Latest frame | `/home/arduino/vlm/latest.jpg` |
| Model weights | `/home/arduino/models/llamacpp/unsloth/Qwen3.5-2B-GGUF/` |
