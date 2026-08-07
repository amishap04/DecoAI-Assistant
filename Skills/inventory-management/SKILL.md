---
name: inventory-management
description: Inventory intake and stock checks against the shared DecoAI SQLite DB. Use whenever an invoice is shared, a decoration photo needs checking against stock, or the owner asks what is on the shelf, what is low, or what to reorder — always run the CLI, never answer stock questions from memory.
---

# SKILL.md — inventory-management

## Overview

| Field       | Details                                                                 |
|-------------|-------------------------------------------------------------------------|
| **Name**    | inventory-management                                                     |
| **Type**    | Inventory CRUD + Invoice/Vision Intake (Shared SQLite DB)               |
| **Scripts** | `cli/invoice_cli.py`, `cli/image_cli.py`, `cli/refresh_cli.py`, `cli/clear_cli.py` |
| **Hardware**| Shared SQLite DB (`database/decoai.sqlite`) + Arduino Uno Q (optional)  |

**Description:**
Owner-facing inventory intake and stock-check skill. Three standalone CLIs cover the three
steps of the owner workflow — record a purchase, check a decoration photo against stock, and
refresh/reorder from live shelf counts. Each CLI talks to the shared inventory DB directly;
no server needs to be running.

---

## ⚠️ CRITICAL

> Always invoke the CLIs under `inventory-management\cli\`. NEVER call `app/main.py`'s FastAPI
> routes directly when a CLI already covers the task.
> NEVER modify any script.
> NEVER modify the shared DB schema (`database/schema.sql`).

---

## Invocation

### 1. Invoice upload — `decoai-invoice`

Extracts line items from a purchase invoice (image or PDF) and adds/restocks them in inventory.

**Primary flow — agent decides reusability/rent before committing:**

#### Step 1a: Extract only (no DB writes)
```bash
python ".\skills\inventory-management\cli\invoice_cli.py" <invoice-file> --extract-only
```
Prints the extraction JSON (`invoice_date` + `lines`), with `rent_ea: null` on every line.

#### Step 1b: Agent decides reusability and rent, per line
For each line, decide whether the item is reusable/rentable decor (lighting, furniture, linens,
structural pieces) vs. a consumable/one-time item (food, disposables, single-use favors). There
is no fixed formula — judge a sensible per-rental price from the item type, its `cost_ea`, and
typical event-rental market norms. Set `rent_ea` to that judged price if reusable, or `0` if not.
Write the finalized JSON (same shape, `rent_ea` filled in) to a scratch file.

#### Step 1c: Commit the finalized data
```bash
python ".\skills\inventory-management\cli\invoice_cli.py" --commit-file <scratch-file> [--json]
```
Upserts every line into the shared DB using the agent-decided `rent_ea` values.

> Reusability is **implicit**: `rent_ea > 0` means reusable/rentable, `rent_ea = 0` means not.
> There is no separate `is_reusable` column.
>
> Omitting `rent_ea` on a line (rather than setting it to `0`) leaves an existing item's current
> rent price untouched on restock — only set it when you've actually made a rent decision this
> round.

**Manual / one-shot flow** (extract and commit immediately, skips the reasoning step):
```bash
python ".\skills\inventory-management\cli\invoice_cli.py" <invoice-file> [--json]
```

#### Example
```bash
python ".\skills\inventory-management\cli\invoice_cli.py" "C:\Users\HCKTest\Downloads\invoice.pdf" --extract-only
```

#### Arguments

| Argument       | Flag             | Required | Description                                             |
|----------------|------------------|----------|----------------------------------------------------------|
| Invoice file   | *(pos.)*         | ✅ Yes (unless `--commit-file` used) | Path to the invoice image or PDF |
| JSON output    | `--json`         | ❌ No    | Emit JSON instead of a text summary (one-shot/commit modes only) |
| Extract only   | `--extract-only` | ❌ No    | Extract and print JSON; no DB writes                     |
| Commit file    | `--commit-file`  | ❌ No    | Path to a finalized extraction JSON to commit; no invoice file needed with this flag |

---

### 2. Decoration photo check — `decoai-image`

Detects decoration items in a photo and checks each one against the shared inventory DB
(present / partial / missing).

#### Command
```bash
python ".\skills\inventory-management\cli\image_cli.py" <image-file> [--json] [--missing-only]
```

#### Example
```bash
python ".\skills\inventory-management\cli\image_cli.py" "C:\Users\HCKTest\Pictures\decor.jpg" --missing-only
```

#### Arguments

| Argument       | Flag              | Required | Description                                              |
|----------------|-------------------|----------|------------------------------------------------------------|
| Image file     | *(pos.)*          | ✅ Yes   | Path to the decoration photo                              |
| JSON output    | `--json`          | ❌ No    | Emit full JSON instead of a text summary                  |
| Missing only   | `--missing-only`  | ❌ No    | Emit just the `missing_items` JSON array (for the Amazon URL Builder) |

---

### 3. Shelf refresh — `decoai-refresh`

Syncs shelf stock from the Arduino vision counts, then reports items at/below the reorder
threshold.

Counts come from the **Arduino Uno Q** first — the `unoq` skill captures the shelf on the
ESP32-CAM and its on-board Qwen VLM returns a per-bin count — then from an HTTP vision service at
`ARDUINO_URL`, then from dummy data. Whichever source runs, the counts are written into the same
shared DB. Uno Q counting takes 1–3+ minutes; let it finish.

#### Command
```bash
python ".\skills\inventory-management\cli\refresh_cli.py" [--json] [--dry-run] [--unoq] [--dummy] [--reorder-only]
```

#### Example
```bash
python ".\skills\inventory-management\cli\refresh_cli.py" --unoq
python ".\skills\inventory-management\cli\refresh_cli.py" --dummy --reorder-only
```

#### Arguments

| Argument        | Flag              | Required | Description                                          |
|-----------------|-------------------|----------|-------------------------------------------------------|
| JSON output     | `--json`          | ❌ No    | Emit JSON instead of a text summary                  |
| Dry run         | `--dry-run`       | ❌ No    | Show counts that would be applied; don't touch the DB |
| Force Uno Q     | `--unoq`          | ❌ No    | Require the Uno Q VLM counts; exit 1 instead of falling back |
| Force dummy     | `--dummy`         | ❌ No    | Use generated dummy counts even if the Uno Q or `ARDUINO_URL` is available |
| Reorder only    | `--reorder-only`  | ❌ No    | Emit just the reorder-items JSON array (for the Amazon URL Builder) |

---

### 4. Clear the inventory — `decoai-clear`

Deletes every row from the shared DB (`DELETE FROM items`), leaving the schema intact so the next
invoice or refresh starts from empty. Takes a timestamped backup of the DB file first.

#### Command
```bash
python ".\skills\inventory-management\cli\clear_cli.py" --yes [--json] [--no-backup]
```

#### Example
```bash
python ".\skills\inventory-management\cli\clear_cli.py" --yes
```

#### Arguments

| Argument     | Flag          | Required | Description                                          |
|--------------|---------------|----------|-------------------------------------------------------|
| Confirm      | `--yes`       | ✅ Yes   | Confirms the wipe; without it the CLI refuses and exits 2 |
| No backup    | `--no-backup` | ❌ No    | Skip the `.bak-clear-<timestamp>` copy taken before deleting |
| JSON output  | `--json`      | ❌ No    | Emit JSON instead of a text summary                  |

> ⚠️ Destructive and irreversible except from the backup. Only run it when the owner has clearly
> asked to clear, wipe, empty, or reset the inventory — never as cleanup after a failed import,
> and never to "start fresh" on your own initiative. Tell the owner the backup path afterwards.

---

## Trigger Conditions

Invoke this skill when the user:

- Uploads or shares a purchase invoice (image/PDF) → use **`decoai-invoice`**
- Uploads or shares a photo of a decoration setup / inspiration image → use **`decoai-image`**
- Asks to refresh, sync, or check current shelf stock, or what needs reordering → use **`decoai-refresh`**
- Asks to clear, wipe, empty, or reset the inventory database → use **`decoai-clear`**
- Says things like "add this invoice to inventory", "what do we have vs. need for this photo",
  "check the shelf", "what's low on stock", "what should we reorder"
- Asks what is in stock, on hand, available, or already owned for an event — any question whose
  answer lives in the inventory DB

Stock facts come from the DB, never from your own recollection or from earlier turns in the
conversation. If you did not just see CLI output, you do not know the stock level — run the CLI.
Summarizing an invoice the owner shared without committing it to the DB is not completing the
request.

---

## Expected Output

- **`decoai-invoice`**: per-line action (`created` / `restocked`) and resulting quantity; exit
  code 2 if no line items could be extracted
- **`decoai-image`**: per-item `present` / `partial` / `missing` breakdown plus a
  `missing_items` list ready to feed the Amazon URL Builder skill; exit code 2 if nothing detected
- **`decoai-refresh`**: source used (`unoq`, `arduino` or `dummy`), items updated, and a reorder
  list ready to feed the Amazon URL Builder skill; exit code 1 if the forced source is unreachable
- **`decoai-clear`**: rows deleted, rows remaining, and the backup path; exit code 2 if `--yes`
  was not passed, 1 if the backup could not be written (nothing is deleted in that case)

---

## Do NOT

- ❌ Call `app/main.py`'s FastAPI endpoints directly when the matching CLI already covers the task
- ❌ Modify the shared DB schema (`database/schema.sql`)
- ❌ Modify any script
- ❌ Skip this skill because of a perceived error — report the error, do not work around it
