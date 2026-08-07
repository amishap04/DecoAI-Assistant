# DecoAI Assistant

An event-decoration assistant for **Windows on Snapdragon X Elite**. It runs an
OpenClaw agent that generates decor concepts on the device's Hexagon NPU, counts
real shelf stock through an Arduino Uno Q camera, prices a chosen concept
against a shared inventory database, and sends the owner a shopping list.

Most of the work happens locally: Stable Diffusion 2.1 and Qwen2.5-VL both run
on the NPU, and the inventory database is a plain SQLite file. Only the cloud
image backend and the agent model need the network.

---

## What it does

The owner asks for decor ideas in chat. The agent replies with **three concept
images** — one rendered locally on the NPU, two from Cirrascale — and asks which
to build. Once one is picked, a single call detects the items in that image,
checks them against stock, prices what is missing, builds Amazon links, and
pings the owner on Telegram. Separately, pointing the Uno Q camera at the shelf
refreshes the database from what is actually on it.

| Skill | What it does |
|-------|--------------|
| `image-generation` | Three-image concept sets: 1 local SD2.1 on the NPU, 2 on Cirrascale SDXL |
| `inventory-management` | Invoice intake, photo stock-checks, shelf refresh, and a guarded DB wipe |
| `cost-estimation` | Prices have-vs-need per item from the shared database |
| `unoq` | Captures the shelf on an ESP32-CAM and counts it with Qwen on the Uno Q |
| `amazon-url-builder` | Turns a missing-items list into Amazon purchase links |

---

## Requirements

**Hardware.** A Snapdragon X Elite machine running Windows 11 on ARM64. The
Arduino Uno Q and ESP32-CAM are optional — without them, shelf refresh falls
back to an HTTP service or generated data.

**Software.** `setup.ps1` installs all of this for you through winget and npm,
so you normally only need winget present:

- Node.js 22 or newer
- Python 3.12 **and** 3.13, both native ARM64
- Git and Android platform-tools (`adb`)
- `openclaw` and `geniex`, installed globally via npm

**Not included in this repo.** The precompiled Stable Diffusion 2.1 QNN package
is several GB and is distributed separately. It is a single flat folder holding
seven files:

```
metadata.json
text_encoder.onnx      text_encoder_qairt_context.bin
unet.onnx              unet_qairt_context.bin
vae.onnx               vae_qairt_context.bin
```

The `.onnx` files are thin wrappers and the `.bin` files hold the compiled NPU
graphs, so both halves must stay together. Drop that folder into a `models`
directory — either beside `setup.ps1` or in the OpenClaw workspace — and setup
finds it on its own; otherwise it asks for the path. `start.ps1` checks the same
places, so adding the model later works without re-running setup.

Skipping it is fine. Everything else still works, and cloud image generation
covers the gap until you add it.

**API keys.** `ANTHROPIC_API_KEY` for the agent itself and `CIRRASCALE_API_KEY`
for cloud images. Telegram notifications need a bot token and chat id. Anything
left blank degrades to mock data rather than failing.

---

## Setup

### 1. Get the code

```powershell
git clone https://github.com/amishap04/DecoAI-Assistant.git
cd DecoAI-Assistant
```

### 2. Add your keys

```powershell
copy .env.example .env
notepad .env
```

Fill in `ANTHROPIC_API_KEY` and `CIRRASCALE_API_KEY` at minimum. Leave every
path variable blank — setup fills those in from the directory you choose in the
next step. Values are read literally: do not quote them, and do not put a
comment on the same line as a value.

### 3. Run setup

```powershell
.\setup.bat
```

It requests administrator rights (winget needs them) and then walks through the
whole install. It asks two questions: where to put the data directory, and where
your SD2.1 model package lives. Press Enter to accept the defaults or skip.

Expect it to take a while the first time — the ARM64 wheels for
`onnxruntime-qnn` and the model copy are the slow parts.

To skip the prompts entirely:

```powershell
.\setup.bat -DataRoot D:\DecoAI -ModelBins D:\models\sd21_qnn -NonInteractive
```

### 4. Start everything

```powershell
.\start.bat
```

This brings up all four services, waits for each to accept connections, and then
tails the gateway log. Open <http://127.0.0.1:18789> for the control UI. Press
Ctrl+C to shut everything down.

---

## What setup does

It installs the prerequisites, then configures OpenClaw in
`%USERPROFILE%\.openclaw`: skills and system prompts are deployed into the
workspace, and a runtime `openclaw.json` is written with your paths, a freshly
generated gateway token, and any keys from your `.env`.

Everything large or machine-specific goes in the **data directory** you choose,
not in the repo:

```
<data root>\
  models\stable-diffusion-2-1\Model_Bins\   the SD2.1 QNN package
  outputs\                                  generated concept images
  camera-frames\                            frames pulled from the Uno Q
  database\decoai.sqlite                    the shared inventory database
  venvs\decoai-py313\                       skill CLIs
  venvs\sd21-py312\                         the SD2.1 NPU pipeline
  hf-cache\                                 CLIP tokenizer download
  logs\                                     one log per service
```

The two output folders are linked back into the workspace as directory
junctions, so workspace-relative paths like
`Skills/image-generation/output/concept_1.png` keep working while the bytes land
on your data drive.

Setup also **rewrites the hardcoded paths** baked into the skills on the
original build machines, and points the agent's `exec:` commands at the Python
3.13 virtualenv rather than a bare `python`. Finally it records the resolved
layout in `decoai-setup.json`, which is what `start.ps1` reads. That file is
machine-specific and git-ignored.

Setup is safe to re-run. The usual reason to do so is adding an API key: put it
in `.env` and run `.\setup.bat` again to push it through to the workspace.

---

## Services

`start.ps1` launches these in order and waits for each one:

| Port | Service | Notes |
|-------|---------|-------|
| 50002 | SD2.1 session server | Holds the pre-loaded ORT-QNN sessions. First start takes a minute or two |
| 8004 | Amazon URL builder | Node service that builds purchase links |
| 18181 | Geniex | Qwen2.5-VL on the NPU, for reading decoration photos |
| 18789 | OpenClaw gateway | The agent and its control UI |

Anything unavailable degrades rather than blocking: no model package means the
NPU image is skipped, no `geniex` means photo analysis falls back to a cloud
model. Only the gateway is required — if it cannot start, `start.ps1` stops.

Useful flags: `-NoSd21`, `-NoGeniex` and `-NoAmazon` skip individual services,
and `-SessionServerOnly` runs just the SD2.1 server in the foreground when the
gateway is already running elsewhere.

---

## Using the Uno Q camera

The camera path needs the board connected over USB with its VLM running:

```powershell
adb devices                       # the Uno Q must be listed
adb shell 'bash ~/start_vlm.sh'   # start Qwen on the board
```

Then ask the agent to check the shelf. It captures a frame, shows it to you,
and counts that same frame — so you can see what the numbers came from. Counting
takes one to three minutes on the board; that is normal.

Without the board, shelf refresh uses `ARDUINO_URL` if set, and generated counts
otherwise.

---

## Configuration

Every setting lives in `.env`; `.env.example` documents each one. The file you
edit at the repo root is the template. Setup merges it with the paths it
resolves and writes the real runtime file to `<workspace>\Skills\.env`, which is
what the skills load. Your `.env` is git-ignored, so keys never reach the repo.

The most commonly changed values:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | The agent model. Required |
| `CIRRASCALE_API_KEY` | Cloud image generation. Required for concepts 2 and 3 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` | Owner notifications and the Telegram channel |
| `REORDER_THRESHOLD` | Stock at or below this count is flagged for reorder |
| `IMAGE_READ_MODEL_URL` | Set to `http://127.0.0.1:18181/v1` to route photo analysis to local Geniex |
| `ARDUINO_URL` | HTTP shelf-counting service, used when the Uno Q is not attached |

---

## Troubleshooting

**`winget` is not recognised.** Install App Installer from the Microsoft Store,
or install Node, both Pythons and adb by hand and re-run with `-SkipPrereqs`.

**A virtualenv reports `AMD64` instead of `ARM64`.** You have x64 Python
installed. `onnxruntime-qnn` cannot reach the Hexagon NPU from it. Install the
ARM64 build of Python 3.12 and 3.13, delete the `venvs` folder in your data
directory, and re-run setup.

**Local image generation fails but cloud works.** Either the model package is
missing — check that `metadata.json` is in
`<data root>\models\stable-diffusion-2-1\Model_Bins\` — or the session server is
not up. Look at `logs\sd21-session-server.log`.

**`openclaw is not on PATH`.** The global npm install did not take. Run
`npm install -g openclaw` and start again.

**Port 18789 is already in use.** Another gateway is running. Stop it before
starting a new one; `start.ps1` refuses rather than fighting over the port.

**Shelf counts come back as dummy data.** The board is not reachable. Check
`adb devices` and that the VLM is running on it.

Each service writes its own log to the `logs` folder in your data directory;
that is the first place to look when something starts but misbehaves.

---

## Repository layout

```
setup.ps1 / setup.bat    one-time install
start.ps1 / start.bat    launch every service
.env.example             configuration template
Skills/                  the five skills, plus the shared database layer
System/                  agent system prompts and the OpenClaw config template
```

`System/openclaw.runtime.json` is the template for the config that setup
deploys to `%USERPROFILE%\.openclaw\openclaw.json`; its double-underscore
placeholders are filled in at install time. `System/openclaw.json` is the
reference copy of the original config and is not used at runtime.

---

## License

MIT. See [LICENSE](LICENSE).
