-- DecoAI shared inventory database (SQLite, lives on the X Elite PC)
-- Source of truth for both the Inventory Manager and the Cost Estimator.

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name      TEXT    NOT NULL,
    color          TEXT,
    cost_ea        REAL    NOT NULL DEFAULT 0,   -- purchase price per unit
    rent_ea        REAL    NOT NULL DEFAULT 0,   -- rental price per unit
    quantity       INTEGER NOT NULL DEFAULT 0,   -- current stock (synced by Arduino vision)
    last_purchased TEXT,                         -- optional; ISO date (YYYY-MM-DD)
    bin_id         TEXT,                          -- physical shelf bin the Arduino tracks
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_bin_id    ON items(bin_id);
CREATE INDEX IF NOT EXISTS idx_items_item_name ON items(item_name);
