---
name: cost-estimation
description: Itemized have-vs-need cost estimate for a decoration concept, priced from the shared DecoAI inventory DB. Use whenever the owner asks what a setup costs, the budget for an event, or what still needs buying — always run the CLI, never quote figures from your own guesses.
---

# SKILL.md — cost-estimation

## Overview

| Field       | Details                                                                 |
|-------------|--------------------------------------------------------------------------|
| **Name**    | cost-estimation                                                          |
| **Type**    | Decoration Cost Estimate (Shared SQLite DB)                             |
| **Scripts** | `cli/estimate_cli.py`                                                   |
| **Hardware**| Shared SQLite DB (`database/decoai.sqlite`)                             |

**Description:**
Itemized have-vs-need cost estimate for a decoration concept. Given the items a concept needs,
checks the shared inventory DB and reports what's already on the shelf vs. what must be bought.
Pricing is DB-only — items with no known cost in the DB come back with `cost_ea=0` and
`price_source="missing"`; getting a real price for those is not this skill's job. Price
`missing_items` yourself (e.g. web search) and feed them to the Amazon URL Builder. Talks to the
shared inventory DB directly; no server needs to be running.

---

## ⚠️ CRITICAL

> Always invoke the CLI under `cost-estimation\cli\`. NEVER call `app/main.py`'s FastAPI routes
> directly when the CLI already covers the task.
> NEVER modify any script.
> NEVER modify the shared DB schema (`database/schema.sql`).
> NEVER invent or estimate a price for an item this skill reports as `missing` — that's fine to
> do yourself afterward, but the skill's own `cost_ea`/`total_cost` output must stay DB-only.

---

## Invocation

### Cost estimate — `decoai-estimate`

Checks needed items against the shared inventory DB and returns an itemized estimate.

#### Command
```bash
python ".\skills\cost-estimation\cli\estimate_cli.py" '<items-json>' [--json]
python ".\skills\cost-estimation\cli\estimate_cli.py" --file <items.json> [--json]
```

`<items-json>` is a JSON array of `{"item_name": str, "color"?: str, "quantity"?: int}`.

#### Example
```bash
python ".\skills\cost-estimation\cli\estimate_cli.py" "[{\"item_name\":\"balloon\",\"color\":\"red\",\"quantity\":20},{\"item_name\":\"fairy lights\",\"quantity\":3}]" --json
```

#### Arguments

| Argument      | Flag       | Required | Description                                              |
|---------------|------------|----------|------------------------------------------------------------|
| Items JSON    | *(pos.)*   | ✅ Yes* | Inline JSON array of needed items                          |
| Items file    | `--file`   | ✅ Yes* | Path to a JSON file with the items array instead           |
| JSON output   | `--json`   | ❌ No   | Emit JSON instead of a text summary                         |

*Exactly one of the positional argument or `--file` is required.

---

## Trigger Conditions

Invoke this skill when the user:

- Asks for a cost estimate, budget, or quote for a decoration concept
- Asks "what would this cost", "how much do we need to buy for X", "what's the budget for..."
- Has a list of decoration items and wants have-vs-need + cost broken down
- Asks for an event plan, shopping list, or item list that carries a budget or any money figure —
  even when the word "estimate" never appears. Derive the items from the concept and run the CLI.

Every number you present must trace back to CLI output. A plan with invented per-item costs or a
made-up budget breakdown is wrong even when the totals look plausible — build the items JSON, run
`decoai-estimate`, and price only the returned `missing_items` yourself (clearly labelled as your
own pricing, not DB pricing).

---

## Expected Output

- **`decoai-estimate`**: per-item `needed` / `in_stock` / `missing` / `cost_ea` / `line_cost`
  breakdown, a DB-only `total_cost`, and a `missing_items` list ready to feed the Amazon URL
  Builder skill. Lines with no DB cost show `cost_ea=0`, `price_source="missing"`, and are
  excluded from `total_cost` — price those yourself before presenting a final budget to the user.

---

## Do NOT

- ❌ Call `app/main.py`'s FastAPI endpoints directly when the CLI already covers the task
- ❌ Modify the shared DB schema (`database/schema.sql`)
- ❌ Modify any script
- ❌ Report the DB-only `total_cost` as if it already includes priced-out missing items — price
  `missing_items` separately and add that to the total yourself
- ❌ Skip this skill because of a perceived error — report the error, do not work around it
