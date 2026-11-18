"""SQLite-Zugriff für das Wörterbuch. Eine Datei, kein ORM — bewusst simpel."""
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "woerterbuch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word        TEXT    NOT NULL,
    language    TEXT    NOT NULL,
    definition  TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (word, language)
);
CREATE INDEX IF NOT EXISTS idx_entries_prefix ON entries (language, word);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}
