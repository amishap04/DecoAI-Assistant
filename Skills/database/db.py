"""SQLite access layer for the DecoAI shared inventory DB.

Lives in the shared `database/` folder — imported by both the
inventory-management and cost-estimation services, and by the standalone CLIs.
"""
import os
import sqlite3
import sys
from pathlib import Path

# schema.sql is a read-only template (CREATE TABLE IF NOT EXISTS) — safe to read
# from wherever this module physically lives, including inside a PyInstaller bundle.
HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"


def _default_db_path() -> Path:
    """Resolve the persistent DB file location.

    DECOAI_DB_PATH always wins — set it to point every exe/service at one shared
    file (e.g. the repo's database/decoai.sqlite). Without it: a frozen exe
    anchors next to the executable (PyInstaller onefile extracts to a fresh temp
    dir per run, which would silently lose data if we anchored there instead);
    running from source anchors next to this module, as before.
    """
    env = os.environ.get("DECOAI_DB_PATH")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "database" / "decoai.sqlite"
    return HERE / "decoai.sqlite"


DB_PATH = _default_db_path()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # dict-like rows
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create the DB file and tables if they don't exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
