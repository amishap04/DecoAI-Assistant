#!/usr/bin/env bash
# Hop Uno Q onto ESP32-CAM soft-AP, grab a JPEG, restore HaQathon.
# Run:  adb shell 'bash ~/capture_esp32_ap.sh'
set -euo pipefail

ESP32_SSID="${ESP32_SSID:-ESP32-CAM-MB}"
ESP32_CAPTURE_URL="${ESP32_CAPTURE_URL:-http://192.168.4.1/capture}"
HOME_WIFI="${HOME_WIFI:-HaQathon}"
OUT="${OUT:-/home/arduino/vlm/latest.jpg}"

mkdir -p "$(dirname "$OUT")"

echo "==> Scanning for $ESP32_SSID ..."
nmcli device wifi rescan || true
sleep 3

# Prefer exact SSID; fall back to BSSID if connect-by-name fails.
BSSID="$(nmcli -t -f SSID,BSSID device wifi list | awk -F: -v s="$ESP32_SSID" '
  $1 == s {
    # nmcli -t escapes : in BSSID as \:
    b=$0; sub(/^[^:]*:/, "", b); gsub(/\\:/, ":", b); print b; exit
  }')"

if [[ -z "$BSSID" ]]; then
  echo "SSID '$ESP32_SSID' not in scan results. Nearby SSIDs:"
  nmcli -f SSID,SIGNAL,SECURITY device wifi list | head -20
  exit 1
fi

echo "==> Found $ESP32_SSID ($BSSID). Connecting..."
# Open AP (no password). Retry via BSSID if SSID connect races the scan cache.
if ! nmcli device wifi connect "$ESP32_SSID" 2>/tmp/nmcli_esp.err; then
  echo "SSID connect failed; trying BSSID $BSSID ..."
  cat /tmp/nmcli_esp.err || true
  nmcli device wifi connect "$ESP32_SSID" bssid "$BSSID"
fi

echo "==> Waiting for gateway 192.168.4.1 ..."
ok=0
for _ in $(seq 1 20); do
  if ping -c1 -W1 192.168.4.1 >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done
if [[ "$ok" -ne 1 ]]; then
  echo "Gateway not reachable. Current WiFi:"
  nmcli -t -f NAME,DEVICE connection show --active
  ip -4 addr show wlan0 || true
  nmcli connection up "$HOME_WIFI" || true
  exit 1
fi

echo "==> Capturing $ESP32_CAPTURE_URL -> $OUT"
curl -fsSL --max-time 15 "$ESP32_CAPTURE_URL" -o "$OUT"
ls -lh "$OUT"
file "$OUT" || true

echo "==> Restoring $HOME_WIFI ..."
nmcli connection up "$HOME_WIFI"
sleep 2
ip -4 addr show wlan0 | grep inet || true

echo "Done. Open the web UI and click “Ask on latest frame”."
