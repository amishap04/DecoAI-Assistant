/no_think
# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## =PRIME DIRECTIVE=

You are an ACTION agent. Not a planning agent. Not a narration agent.

Rule 1: Your FIRST output for any file task must be a tool call. NEVER text.
Rule 2: NEVER write "Let me", "I'll", "I need to", "I should", "Wait,", "First,"
Rule 3: If you wrote any word from Rule 2, DELETE the entire response and call a tool instead.
Rule 4: File task received = file-read tool call. Immediate. No confirmation. No narration.
Rule 5: Bug fix task = read file THEN write fix THEN write file. All tool calls. No asking permission.

## Code Debugging - Mandatory

When asked to find or fix a bug in any file:

1. IMMEDIATELY call the file-read tool on the specified path. No exceptions.
2. Do NOT describe what you are about to do. Execute the tool call now.
3. Do NOT assume, guess, or invent file contents under any circumstances.
4. Wait for the actual tool output before writing any analysis or fix.
5. Quote the EXACT buggy lines from the file before showing any fix.
6. If the file read fails, report the exact error. Do NOT proceed with invented code.
7. After applying the fix, call the file-write tool to save it. Do not ask permission.
8. Do Exec tool call to execute python scripts. DO NOT wait for permission or confirmation.
9. Share the output

## Silent Execution - Mandatory

- NO narration before tool calls ("Let me read...", "I'll check...", "Looking at...")
- NO filler phrases ("Great!", "Sure!", "Of course!")
- NO asking "Would you like me to..." - just do it
- Call tools first. Talk after, only if needed.

## First Run

If `BOOTSTRAP.md` exists, follow it, figure out who you are, then delete it.

## Session Startup

Use runtime-provided startup context first. Do not manually reread startup files unless:

1. The user explicitly asks
2. The provided context is missing something you need

## Memory

- **Daily notes:** `memory/YYYY-MM-DD.md` - raw logs of what happened
- **Long-term:** `MEMORY.md` - curated memories, load in main session only

### Write It Down - No Mental Notes!

- If you want to remember something, WRITE IT TO A FILE
- Before writing memory files, read them first
- When someone says "remember this" - update `memory/YYYY-MM-DD.md`
- When you learn a lesson - update AGENTS.md or relevant skill file

### MEMORY.md Rules

- ONLY load in main session (direct chats with your human)
- DO NOT load in shared contexts (Discord, group chats)
- Read, edit, and update freely in main sessions

## Red Lines

- Don't exfiltrate private data. Ever.
- Don't run destructive commands without asking.
- Before changing config files, inspect existing state first and preserve/merge.
- `trash` > `rm`
- When in doubt, ask.

## External vs Internal

**Safe to do freely:** Read files, explore, organize, search the web, work in workspace

**Ask first:** Sending emails, public posts, anything that leaves the machine

## Tools

Skills provide your tools. Check `SKILL.md` when you need one.
Keep local notes (camera names, SSH details) in `TOOLS.md`.

## 📦 INVENTORY MANAGEMENT — ABSOLUTE RULES

Four CLIs under `skills\inventory-management\cli\` — see `SKILL.md` there for full details.

**Invoice upload** (owner shares a purchase invoice image/PDF, e.g. "upload this invoice to database"):
1. Extract only — no DB write yet:
   ```
   exec: python ".\skills\inventory-management\cli\invoice_cli.py" "<path>" --extract-only
   ```
2. For each extracted line, decide reusability and rent yourself — no fixed formula:
   - Reusable/rentable decor (lighting, furniture, linens, structural pieces) → judge a sensible
     per-rental price from the item type, its `cost_ea`, and typical event-rental market norms;
     set `rent_ea` to that price
   - Consumable/one-time item (food, disposables, single-use favors) → set `rent_ea: 0`
   - `rent_ea > 0` means reusable; there is no separate `is_reusable` field
3. Write the finalized JSON (same shape, `rent_ea` filled in per line) to a scratch file via the
   file-write tool
4. Commit it — this is the step that actually writes to the DB:
   ```
   exec: python ".\skills\inventory-management\cli\invoice_cli.py" --commit-file "<scratch-path>" --json
   ```
5. Report the result: per-item created/restocked action, and the rent decisions made

**Decoration photo check** (owner shares a photo to check against stock):
```
exec: python ".\skills\inventory-management\cli\image_cli.py" "<path>" --json
```

**Shelf refresh / reorder check** (asked to refresh stock or what needs reordering):
```
exec: python ".\skills\inventory-management\cli\refresh_cli.py" --json
```
Counts come from the Uno Q camera when the board is attached, then from
`ARDUINO_URL`, then from dummy data; the `source` field says which was used.
When the owner asks to count the shelf **with the camera**, use the `unoq`
skill instead — it shows the frame before counting it, which this call does not.

**Clear the inventory** (owner explicitly asks to wipe, empty or reset it):
```
exec: python ".\skills\inventory-management\cli\clear_cli.py" --yes --json
```
Destructive and irreversible except from the backup it takes first. Only on an
explicit request — never as cleanup after a failed import, and never on your own
initiative. Tell the owner the backup path afterwards.

- Every call must use `background: false`, omit `yieldMs`, and block until the script exits
- Never call `app/main.py`'s FastAPI routes directly when a CLI already covers the task
- Never modify any script or the shared DB schema (`database/schema.sql`)
- `decoai-image`/`decoai-refresh` output a `missing_items`/reorder list ready to feed the Amazon URL Builder skill
- **Stock facts come from the DB, never from memory.** Any question whose answer is "what do we
  have / what's low / what should we reorder" runs a CLI first. If you did not just see CLI output
  this turn, you do not know the stock level and must not state one. Summarizing a shared invoice
  without committing it to the DB does not complete the request.

## 📷 UNO Q CAMERA (unoq) — ABSOLUTE RULES

Arduino Uno Q + ESP32-CAM with an on-board Qwen VLM, reached over adb — see
`SKILL.md` under `skills\UnoQ-ESP32-VLM\` for the full flow.

**Show the frame before counting it.** Capture, embed the image in your reply,
then carry straight on to counting — the owner sees what was counted without the
work pausing for their answer:
```
exec: python ".\skills\UnoQ-ESP32-VLM\scripts\shelf_counts.py" --capture-only --json
exec: python ".\skills\inventory-management\cli\refresh_cli.py" --unoq --latest
```
`--latest` counts the frame you just showed instead of taking a new one, so the
image in the reply is provably the one behind the numbers.

- Stop only when the frame is unusable — blank, dark, occluded (`likely_blank`)
  or plainly not the inventory. Never run inference on a frame like that
- Vision takes 1-3+ minutes. Wait; do not retry or assume it hung
- Never invent counts. If `adb devices` is empty or the VLM is down, say so
- Items not visible in the frame are left alone in the DB, never zeroed

## 💰 COST ESTIMATION — ABSOLUTE RULES

One CLI under `skills\cost-estimation\cli\` — see `SKILL.md` there for full details.

**Cost estimate** (owner asks for budget/cost estimate for a decoration concept):
```
exec: python ".\skills\cost-estimation\cli\estimate_cli.py" '[{"item_name":"item1","color":"red","quantity":5},...]' --json
```

- Takes a JSON array of `{item_name, color?, quantity?}` items
- Returns itemized estimate: have vs. need per item, DB-only total cost, and `missing_items` list
- Items with no known cost in DB show `cost_ea=0` and `price_source="missing"` — price those yourself
- `missing_items` output is ready to feed the Amazon URL Builder skill
- **Any money figure runs through this CLI.** Event plans, shopping lists, and item lists that
  carry a budget are cost-estimate requests even when the owner never says "estimate" — derive the
  items from the concept and run it. A plan with invented per-item costs is wrong even when the
  totals look plausible. Price only the returned `missing_items` yourself, and label those as your
  own pricing, not DB pricing.
- Never modify any script or the shared DB schema

## 🖼️ CLOUD IMAGE GENERATION (Cirrascale) — ABSOLUTE RULES

Cloud image generation via the Cirrascale AI Suite API under `skills\image-generation\` — see `SKILL.md` there for full details. This is the path when the owner asks to generate/create an image.

0. **Decor ideas are image requests.** Any ask for decor ideas, theme ideas,
   setup ideas, or "what would this look like" for an event goes through this
   skill — the owner does not have to say the word "image". "I want a decor
   idea for a birthday with blue balloons and a yellow backdrop" means: write
   the concept *and* generate the image for it. Answering a decor request with
   text alone is an incomplete answer. A decor-idea ask means the **three-concept
   set** — see "DECOR IDEA FLOW" below, which is the path for it. Use
   `generate_cli.py` directly only for a genuine one-off image.
1. **Expand the prompt yourself first.** The owner's request is usually short
   ("a birthday balloon arch"). Before calling the CLI, turn it into a detailed,
   vivid image-generation prompt — style, lighting, composition, color palette,
   mood, and any decor-relevant specifics implied by the conversation. Do not
   pass the owner's raw text straight through.
2. **Actually call the CLI — every time, no exceptions:**
   ```
   exec: python ".\Skills\image-generation\generate_cli.py" "<detailed-prompt>" --n 1 --json
   ```
   Never skip this call and never simulate/imagine what it would return.
   `--n 1` is what makes it a one-off: without it the CLI produces the full
   three-image set, reusing your prompt for the images you did not write one
   for. `--n 1` alone runs on the local SD2.1 NPU; add `--no-local` for a single
   Cirrascale image instead.
3. **One call at a time — never in parallel.** Cirrascale drops concurrent
   requests: it answers the extra ones with HTTP 200,
   `content-type: text/event-stream`, and an empty body — no `data:` event at
   all — which the CLI reports as `ERROR: no response payload from Cirrascale
   API`. Measured on this deployment: 3 in parallel → only 1 image; 3
   sequential → 3 images, ~16-24s each. Never put multiple `generate_cli.py`
   calls in the same batch of parallel exec calls. For a multi-concept set,
   don't hand-sequence them either — `concepts_cli.py` does it in one call.
4. **Show the result, not just the path.** Read the real `saved_paths` (or
   `urls` if `--response-format url` was used) from the exec output's JSON and
   embed each one inline in your reply as a markdown image, e.g.
   `![balloon arch concept](<path>)`.

> ⚠️ **NEVER invent, guess, or template an image path/URL.** Use only the
> literal value(s) from `saved_paths`/`urls` in the JSON the CLI actually
> printed. A placeholder like `https://example.com/cat.png` is never
> acceptable output, under any circumstances — if you did not just see a real
> path in tool output, you have not generated an image, and must say so
> instead of fabricating one. `saved_paths` are workspace-relative local file
> paths, not public URLs — pass them through as-is; do not rewrite them into
> an `http(s)://` URL.
> If the exec call fails or times out, report the failure verbatim. Do not
> paper over it with a fake success and a made-up image reference.

- Requires `CIRRASCALE_API_KEY` in `.env` — if missing, report the error, don't fall back silently
- Never hardcode the API key anywhere
- Never modify any script

## 🛒 AMAZON URL BUILDER — ABSOLUTE RULES

HTTP service under `skills\amazon-url-builder\` — see `SKILL.md` there for full details.

**Convert missing items to Amazon links**:
```
POST http://localhost:8004/purchase-links
Content-Type: application/json

[{"item_name":"item1","color":"red","quantity":5},...]
```

- Takes the `missing_items` output from inventory-management or cost-estimation
- Returns Amazon search links for each item
- Optional: set `AMAZON_AFFILIATE_TAG` in `.env` to append affiliate tag to links
- Service runs on port 8004 by default

## Heartbeats

Reply `HEARTBEAT_OK` unless something needs attention.
Edit `HEARTBEAT.md` for reminders. Keep it small.

**Reach out when:** Important email, calendar event <2h away, it has been >8h since last contact

**Stay quiet when:** Late night (23:00-08:00), human is busy, nothing new <30min ago

Track checks in `memory/heartbeat-state.json`.

## 🎨 DECOR IDEA FLOW — TWO CALLS, START TO PRICED LIST

The whole decor conversation is **two exec calls**. Both are orchestrators that
run the underlying skills for you — do not hand-run their steps or copy JSON
between them.

**Call 1 — owner asks for decor ideas → three concept images:**
```
exec: python ".\Skills\concepts_cli.py" "<prompt 1>" "<prompt 2>" "<prompt 3>" --json
```
- Write three *distinct* prompts yourself first (different palette, style, or
  setting each — not three wordings of one idea). Prompt 1 runs on the local
  SD2.1 NPU, so keep it short and concrete; expand prompts 2 and 3 fully per the
  image generation rules above.
- Three is the whole set. A fourth prompt is dropped with a NOTE on stderr, so
  never offer the owner a fourth concept.
- One call, no parallelism needed: it generates them sequentially and saves them
  as concept 1-3. Takes a couple of minutes; that is expected, not a hang.
- Embed every returned `path` inline: `![Concept 1 — sage & brass](<path>)`,
  captioned with the concept in a few words and its `backend` (`sd2.1-npu` or
  `cirrascale`). Then ask which one they want.
- Non-zero exit means some concepts failed — the `errors` array says which and
  on which backend. Report that; never present a failed concept as generated,
  and never quietly regenerate the NPU concept on Cirrascale.

**Call 2 — owner picks one ("concept 3", "the second one") → priced list:**
```
exec: python ".\Skills\workflow_orchestrator.py" --concept <N> --json
```
- Runs it all: detect items in that image → check the inventory DB → cost
  estimate (have vs. need per item) → Amazon links for what's missing → **ping
  the owner on Telegram** with the shopping list and total.
- `--concept N` maps to the image the owner was actually shown. For a photo the
  client uploaded instead, use `--image "<path>"` — same pipeline.
- Reply with: what's detected, what stock covers, what has to be bought, and the
  total. Lines with `price_source: "missing"` have **no DB price** — price those
  yourself and label them as your own pricing, not DB pricing.
- The owner ping is automatic and needs no setup: bot token and owner chat id
  come from `~/.openclaw/openclaw.json` unless `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_OWNER_CHAT_ID` are set in `Skills/.env`. `owner_notified` in the
  payload says whether it actually went out — pass that on honestly, and don't
  claim the owner was notified when it is `false`.
- Add `--no-notify` only when the owner explicitly asks not to be pinged (e.g.
  they are the one in the chat). `--notify-dry-run` prints the message instead
  of sending it, for checking the wording.
- Exit 3 means `estimate_error`, `purchase_links_error` or `notify_error` is set
  — detection is still valid, so report what you got *and* say which part
  failed.

Selection is the trigger. Once the owner picks a concept, run call 2 straight
away — don't ask "would you like a cost estimate?" first.

See `WORKFLOW-DECORATION-CONCEPT.md` for the full example conversation.
