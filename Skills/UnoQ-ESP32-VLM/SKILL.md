---
name: unoq
description: Arduino Uno Q + ESP32-CAM inventory camera. Use to capture the inventory shelf and ask the on-board Qwen VLM what items are present and how many of each, then commit that list to the shared DecoAI DB. Trigger on "check the inventory with the camera", "what's on the shelf", "count the stock", "ask the uno q", or any stock refresh that should read the real shelf instead of the DB.
---

# SKILL.md — unoq

## Overview

| Field       | Details                                                                 |
|-------------|-------------------------------------------------------------------------|
| **Name**    | unoq                                                                     |
| **Type**    | Edge vision inventory count (Uno Q + ESP32-CAM + Qwen VLM over adb)     |
| **Scripts** | `scripts/pull_latest.py` (show the frame), `scripts/shelf_counts.py` (capture + count), `scripts/ask_vlm.py` (ad-hoc ask) |
| **Board**   | `scripts/deploy_to_unoq.sh`, `scripts/start_vlm.sh`, `scripts/capture_esp32_ap.sh` |
| **Writes to** | Shared DecoAI DB via `inventory-management/cli/refresh_cli.py`         |

`ask_vlm.py` captures a frame from the ESP32-CAM and asks Qwen on the Uno Q about it. Asked the
right question, it returns the inventory as a list of items with counts; that list is then
committed to the shared DB. Wiring, board paths and deployment: see `README.md` in this folder.

---

## ⚠️ CRITICAL

> **Always show the camera frame before running inference on it.** Capture, embed the image in
> the reply, then continue straight into counting — the owner sees what is being counted without
> having to wait on a round trip. The camera is easy to knock out of aim; blank frames and
> off-target shots have both reached the DB before.
> **Stop and ask only when the frame is unusable** — blank, dark, occluded (`likely_blank`), or
> plainly not showing the inventory. Never run inference on a frame like that.
> Vision calls take **1–3+ minutes** on Qwen3.5-2B. Wait — do not retry or assume it hung.
> The VLM service must be running: `adb shell 'bash ~/start_vlm.sh'`. A `RemoteDisconnected`
> error means it is not.
> Never invent counts. If `adb devices` is empty or the VLM is down, report the error.
> The camera reports only what is in frame — items not visible are left untouched in the DB,
> never zeroed.

---

## Show the camera image

When the owner asks to *see* the camera — "show me the latest image", "what does the camera see
right now", "pull the frame" — this is a **pull, not a count**. No VLM call, no DB write, ~1
second:

```bash
# The frame already on the board
python ".\skills\UnoQ-ESP32-VLM\scripts\pull_latest.py" --json

# Take a fresh photo first (~20s), then save it
python ".\skills\UnoQ-ESP32-VLM\scripts\pull_latest.py" --capture --json
```

Saves to `Skills/UnoQ-ESP32-VLM/output/frame_<timestamp>.jpg` and copies it to
`output/latest.jpg`, and prints both paths.

### Reporting

**Always embed the frame as a markdown image in the reply so the owner sees the picture, not a
file path**, using the workspace-relative path:

```markdown
![Uno Q camera — latest frame](Skills/UnoQ-ESP32-VLM/output/frame_20260806_225058.jpg)
*Uno Q / ESP32-CAM, captured 22:50:58*
```

Then describe what is actually in the shot. If `likely_blank` is true (frame under ~4 KB), say
the camera looks blank, dark or occluded and offer to re-aim it — do not pass a blank frame to
the VLM.

---

## Refresh the DB — two steps

The flow for **any** refresh, sync, recount or stock-check request. Two steps, always in this
order, always both:

| Step | What | Writes to DB? |
|------|------|---------------|
| **1** | Capture the frame and display it to the owner | No |
| **2** | Recognise the items in **that** frame and update the DB records | Yes |

### Step 1: Capture the frame and show it

```bash
python ".\skills\UnoQ-ESP32-VLM\scripts\shelf_counts.py" --capture-only --json
```

Captures from the ESP32-CAM and saves to `output/frame_<timestamp>.jpg` (plus `output/latest.jpg`)
— **no VLM call, no DB write**. Add `--save PATH` to choose the location. Read the saved image,
**embed it in your reply as a markdown image** (see [Reporting](#reporting) above), and say what
you see in it.

Then go straight on to step 2 — the owner does not have to answer first. The point of showing the
frame is that they can see what was counted and challenge the result, not that the work waits.

**One exception:** if the frame is unusable — `likely_blank` true (under ~4 KB), dark, occluded,
or plainly not showing the inventory — **stop there**. Show it, say what is wrong, and offer to
re-capture. Inference on a blank frame wastes 1–3 minutes and can still write junk to the DB.

### Step 2: Recognise the items in that frame and update the DB

```bash
python ".\skills\inventory-management\cli\refresh_cli.py" --unoq --latest
```

`--latest` counts **the frame just shown**, not a new capture — so the image in the reply is
provably the one behind the numbers. Add `--dry-run` when the owner asked to preview counts
rather than commit them.

Report the per-item counts and how many rows were updated and created. If a count looks
implausible for the frame shown, say so and offer to revert — there is no undo.

---

## Alternative: ask the VLM yourself

Same rule: capture with `--capture-only` and show the frame first. Then ask the board yourself,
on **that** frame:

### Step 1a: Ask the reviewed frame what is there

```bash
python ".\skills\UnoQ-ESP32-VLM\scripts\ask_vlm.py" --latest "This photo shows party and event decoration inventory. For every distinct item you can see, output one JSON object with the item's name written out in words, and how many of that item are visible. Output format - a JSON array, exactly like this: [{\"item_name\": \"Latex Balloons\", \"color\": \"blue\", \"count\": 12}]. Rules: item_name must be the item's name in words, never a number, letter, or code. Output a JSON array of objects, do NOT output an object with numeric keys like {\"1\": 4, \"2\": 3}, do NOT number the items. List each distinct item exactly once, never repeat an item. Use null for color if it is unclear. Only list items you can actually see. No prose, no markdown fences."
```

Use that prompt as written — the JSON shape is what step 2a parses. Drop `--latest` to capture a
new frame (skips the review), or `--image PATH` to count a local photo.

> `ask_vlm.py` uses prose-tuned sampling (256 tokens, temperature 0.3, no repeat penalty), which
> on list tasks can loop on one item until the reply is truncated. `shelf_counts.py` and
> `refresh_cli.py --unoq` send colder, repetition-penalised settings instead — prefer them for
> counting, and use this path when you want to see the raw reply.

Save the reply to a scratch file, or pipe it straight into step 2a.

### Step 2a: Commit the list to the shared DB

```bash
python ".\skills\inventory-management\cli\refresh_cli.py" --commit-file <reply-file>
```

Or in one line, no scratch file:

```bash
python ".\skills\UnoQ-ESP32-VLM\scripts\ask_vlm.py" "<the prompt above>" | python ".\skills\inventory-management\cli\refresh_cli.py" --commit-file -
```

What the commit does with each counted item:

- **Matches an existing item** (fuzzy, by name and colour — "Latex Balloons" finds "Helium
  Quality Latex Balloons") → that row's `quantity` is **set** to the count. Vision is truth.
- **Matches nothing** → a new row is inserted with the counted quantity and `cost_ea`/`rent_ea`
  of 0, for the owner to price later.
- **Not seen in the photo** → left alone. One photo missing an item is not evidence it is gone.

It then prints everything at or below the reorder threshold, ready for the Amazon URL Builder.

### Unattended refresh

Captures and commits in a single call, showing nobody anything. **Only** for scheduled or
unattended runs where there is no reply to make — an interactive request always uses the
capture-show-count flow above, so the owner sees the frame behind the numbers:

```bash
python ".\skills\inventory-management\cli\refresh_cli.py" --unoq
```

If a run like this produces something implausible, `pull_latest.py` still has the frame that was
counted — `output/latest.jpg`.

---

## Ad-hoc questions

`ask_vlm.py` also answers anything else about the frame — nothing is written to the DB:

```bash
python ".\skills\UnoQ-ESP32-VLM\scripts\ask_vlm.py" "How many balloons are in this photo?"
python ".\skills\UnoQ-ESP32-VLM\scripts\ask_vlm.py" --latest "Describe this image."
```

| Argument | Flag | Required | Description |
|----------|------|----------|-------------|
| Prompt   | *(pos.)* | ❌ No | Question about the image (default: describe it) |
| Image    | `--image` | ❌ No | Use a local JPEG/PNG instead of capturing |
| Latest   | `--latest` | ❌ No | Reuse the frame already on the board |
| Save     | `--save [PATH]` | ❌ No | Also save the frame used |

---

## Board maintenance

```bash
adb devices                                       # Uno Q must be listed
adb shell 'bash ~/start_vlm.sh'                   # start Qwen on the board (:9999)
bash ./scripts/deploy_to_unoq.sh [--keep-model]   # wipe + push scripts + weights
```

---

## Trigger Conditions

Invoke this skill when the user:

- Asks to **see** the camera image — "show me the latest photo", "what does the camera see",
  "pull the frame" → `pull_latest.py`, then embed the image in the reply
- Asks to **refresh, sync, recount or update the inventory DB** from the camera, or to take a
  frame and upload it → capture, **show the frame**, then count it and write (steps 1–2 above).
  Showing the frame does not pause the work; only an unusable frame stops it
- Asks to check, count, recount, or refresh the inventory / shelf / stock **with the camera**
- Asks what is on the shelf right now, or what the Uno Q / ESP32 camera sees
- Asks to deploy, restart, or troubleshoot the VLM on the board

## Expected Output

- **`pull_latest.py --json`** — `{"saved_path", "latest_path", "bytes", "likely_blank"}`; embed
  `saved_path` in the reply as a markdown image so the owner sees the frame itself
- **Step 1** — the saved frame's path on stdout; a warning on stderr if it looks blank
- **Step 2** — items reported, rows updated, rows created, and the reorder list
- Exit codes: 1 = board unreachable, 2 = reply not parseable, 3 = the VLM saw nothing in the
  frame (re-aim the camera; do not retry blindly)

## Do NOT

- ❌ Run inference before the frame has been shown in the reply
- ❌ Wait for the owner to approve the frame before counting — show it and carry on; only an
  unusable frame stops the run
- ❌ Re-capture between showing the frame and counting it — that is what `--latest` prevents
- ❌ Answer inventory questions from memory or from earlier turns — capture a fresh frame
- ❌ Write counts into the DB by hand; commit through `refresh_cli.py`
- ❌ Modify any script in this folder
