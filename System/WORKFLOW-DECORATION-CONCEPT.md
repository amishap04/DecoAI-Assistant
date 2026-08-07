# Decor Idea Workflow

Owner asks for decor ideas → three concept images → owner picks one → detected
items, stock check, missing list and cost estimate come back automatically.

Two exec calls carry the whole flow. Each is an orchestrator that runs the
underlying skills in-process, so the agent never copies JSON from one tool call
into the next — that hand-off is where this flow used to break.

## Flow

1. **Owner asks for decor ideas** (OpenClaw chat / Telegram)
   - Agent writes three distinct image prompts. Prompt 1 runs on the local
     SD2.1 NPU, so keep it short and concrete; prompts 2 and 3 go to Cirrascale
     and should be fully detailed
   - `concepts_cli.py` generates all three sequentially in one call
   - Agent embeds the three images inline, labels each with its backend, and
     asks which one to build

2. **Owner picks a concept** ("concept 3", "the second one")
   - `workflow_orchestrator.py --concept N` runs, with no further prompting:
     detect items → check inventory DB → cost estimate → Amazon links →
     Telegram ping to the owner
   - Agent replies with the itemized have-vs-need breakdown and total

Selection is the trigger for step 2 — the agent does not ask permission to
price a concept the owner just chose.

## Call 1 — generate the concept set

```bash
python ".\Skills\concepts_cli.py" "<prompt 1>" "<prompt 2>" "<prompt 3>" --json
```

Output:
```json
{"concepts": [{"concept": 1, "backend": "sd2.1-npu", "prompt": "...", "path": "Skills/image-generation/output/concept_1786078633_sd21.png", "abs_path": "..."}],
 "errors": []}
```

- Three is the whole set. A fourth prompt is dropped with a NOTE on stderr —
  ask for three, and never promise the owner a fourth image
- Saves the NPU concept as `concept_<ts>_sd21.png` and each cloud concept as
  `concept_<ts>_<n>.png`, and records the run in
  `Skills/image-generation/output/concepts_latest.json`
- Sequential by necessity: Cirrascale answers concurrent requests with an empty
  event stream. ~16-24s per cloud concept; the NPU one is slower
- Exit 0 = all three generated, 2 = some failed (`errors` says which and on
  which backend), 3 = all failed. Embed `path` values verbatim; never invent one
- `backend` is `sd2.1-npu` or `cirrascale` — label each image with it in the
  reply, and never regenerate a failed NPU concept on Cirrascale instead
- Options: `--no-local` (all three on Cirrascale), `--seed N`, `--size`,
  `--steps`, `--model`, `--timeout` (per concept, default 120s)

## Call 2 — price the chosen concept

```bash
python ".\Skills\workflow_orchestrator.py" --concept 3 --json
python ".\Skills\workflow_orchestrator.py" --image "<path>" --json   # client-uploaded photo
```

Runs, in order:

| Step | Tool | Gives |
|---|---|---|
| detect items | `inventory-management/cli/image_cli.py` | `results`, `items_detected` |
| stock check | same call | `missing_items` (shortfalls) |
| cost estimate | `cost-estimation/cli/estimate_cli.py` | `estimate.lines`, `estimate.total_cost` |
| purchase links | `amazon-url-builder/src/builder.js` | `purchase_links` |
| notify owner | Telegram bot API `sendMessage` | `owner_notified`, `notify` |

Output:
```json
{"image": "...concept3_....png",
 "items_detected": 5,
 "results": [{"item_name": "Fairy Lights", "color": "warm white", "detected_quantity": 3, "in_stock": 0, "shortfall": 3, "status": "missing"}],
 "missing_items": [{"item_name": "Fairy Lights", "color": "warm white", "quantity": 3}],
 "estimate": {"lines": [{"item_name": "Fairy Lights", "needed": 3, "in_stock": 0, "missing": 3, "cost_ea": 8.99, "line_cost": 26.97, "price_source": "db"}],
              "total_cost": 26.97,
              "missing_items": [...]},
 "purchase_links": [{"search_terms": "warm white Fairy Lights", "quantity": 3, "url": "https://www.amazon.com/s?k=..."}],
 "owner_notified": true,
 "notify": {"sent": true, "chat_id": "8701252343", "text": "🛒 Decor concept confirmed: …"}}
```

- `--concept N` resolves through `concepts_latest.json`, so it picks the image
  the owner was actually shown rather than a same-numbered leftover from an
  earlier set; it falls back to the newest `conceptN_*.png`
- The estimate prices **every** detected item, not just the shortfalls, so the
  reply can show what stock already covers next to what must be bought
- `price_source: "missing"` means the DB has no cost for that item —
  `total_cost` excludes it, and the agent prices it itself and says so
- Exit 0 = success, 1 = bad input / no such concept, 2 = nothing detected,
  3 = pricing, link building or the owner ping failed (`estimate_error` /
  `purchase_links_error` / `notify_error` in the payload; detection results are
  still valid and still printed)

### Step 5 — the owner ping

The owner gets the shopping list on Telegram as soon as a concept is chosen:

```
🛒 Decor concept confirmed: concept3_1786028168_0.png

3 item(s) to purchase:
• warm white Fairy Lights x3 — $26.97
• red Paper Lantern x6 — $7.20
• ivory Table Runner x2 — $11.00

💰 Total (DB-priced): $45.17
```

- Credentials resolve from `TELEGRAM_BOT_TOKEN` / `TELEGRAM_OWNER_CHAT_ID` in
  `Skills/.env` if set, otherwise from `~/.openclaw/openclaw.json`
  (`channels.telegram.botToken` and the `telegram:<id>` in
  `commands.ownerAllowFrom`) — so it works with no extra setup
- Items with no DB price show as "price TBD"; lists over 30 items are trimmed
- `--no-notify` skips the send; `--notify-dry-run` prints the message instead of
  sending it
- A failed send never discards the run: `owner_notified` goes `false`,
  `notify_error` explains why, and everything else still reports

## Example

**Owner:** "Give me decor ideas for a 30th birthday, gold and navy"

**Agent:** [one `concepts_cli.py` call, three prompts] → posts three images

> ![Concept 1 — gold balloon arch, navy drape](Skills/image-generation/output/concept_..._sd21.png)
> *local SD2.1 on the NPU*
> ![Concept 2 — navy tablescape, brass candlesticks](...) *Cirrascale*
> ![Concept 3 — gold fringe backdrop, navy florals](...) *Cirrascale*
>
> "Which one do you want to build?"

**Owner:** "Concept 3"

**Agent:** [one `workflow_orchestrator.py --concept 3` call]

> "Concept 3 needs 5 items. Stock covers the silk roses (20) and 20 of the gold
> balloons. To buy:
> - Fairy Lights x3 — $8.99 ea = $26.97
> - Paper Lanterns x6 — $1.20 ea = $7.20
> - Table Runners x2 — $5.50 ea = $11.00
>
> **Total: $45.17** (DB pricing). Amazon links: …
> Shopping list sent to the owner on Telegram."

## Requirements

- `CIRRASCALE_API_KEY` in `Skills/.env` for image generation
- `IMAGE_READ_MODEL_URL` (the Geniex vision server on :18181, started by
  `start.ps1`) for item detection; falls back to Claude if unset with an API
  key, and to a mock if neither is configured
- `node` on PATH for the Amazon link step; optional `AMAZON_AFFILIATE_TAG`
- A Telegram bot token and owner chat id for the ping — already present in
  `~/.openclaw/openclaw.json`, override in `Skills/.env` to notify a different
  chat
