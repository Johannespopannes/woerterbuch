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
    image       TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (word, language)
);
CREATE INDEX IF NOT EXISTS idx_entries_prefix ON entries (language, word);
CREATE TABLE IF NOT EXISTS ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    INTEGER NOT NULL REFERENCES entries (id),
    understood  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ratings_entry ON ratings (entry_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Nachträgliche Spalten für bestehende DBs (CREATE TABLE ändert sie nicht)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info (entries)")}
    if "image" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN image TEXT")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    # SQLites LOWER()/LIKE-Fold ist nur ASCII — für Umlaute (Ö vs. ö)
    # eine echte Unicode-Variante aus Python registrieren.
    conn.create_function(
        "ULOWER", 1, lambda s: s.lower() if isinstance(s, str) else s
    )
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}
