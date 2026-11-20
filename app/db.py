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
CREATE TABLE IF NOT EXISTS search_stats (
    term        TEXT    NOT NULL,
    language    TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (term, language)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Nachträgliche Spalten für bestehende DBs (CREATE TABLE ändert sie nicht)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info (entries)")}
    if "image" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN image TEXT")
    if "lemma" not in cols:
        # Grundform + Flexionsform, damit Wortfamilien sichtbar werden
        conn.execute("ALTER TABLE entries ADD COLUMN lemma TEXT")
    if "form" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN form TEXT")
    if "confidence" not in cols:
        # 0..1 Wahrscheinlichkeit, dass das Wort zur Sprache gehoert
        # (NULL = noch nicht geprueft)
        conn.execute("ALTER TABLE entries ADD COLUMN confidence REAL")
    if "evidence" not in cols:
        # JSON-Blob: Pruef-Evidenz (Woerterbuch-Treffer, Wikipedia,
        # Modellurteil, Beispielsaetze, Votes)
        conn.execute("ALTER TABLE entries ADD COLUMN evidence TEXT")
    if "sprache_unsicher" not in cols:
        # 1 = beanstandet; wird markiert statt geloescht
        conn.execute("ALTER TABLE entries ADD COLUMN sprache_unsicher "
                     "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS word_ratings ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " fits INTEGER NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_word_ratings_entry "
        "ON word_ratings (entry_id)")
    # Galerie: mehrere Bilder beschreiben ein Wort gemeinsam.
    # Das Hauptbild bleibt in entries.image und ist hier ebenfalls
    # verzeichnet (UNIQUE verhindert Doppelungen).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entry_images ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " filename TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (entry_id, filename)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entry_images_entry "
        "ON entry_images (entry_id)")
    # Bild-Votes pro Datei: NULL (bzw. leer) = Stimme fuer das Hauptbild.
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info (ratings)")}
    if "image" not in rcols:
        conn.execute("ALTER TABLE ratings ADD COLUMN image TEXT")
    if "kind" not in rcols:
        # 'image' = Bildpasst-Stimme, 'unknown' = "Wort unbekannt".
        # Unbekannt-Stimmen zaehlen NICHT als schlechtes Bild.
        conn.execute("ALTER TABLE ratings ADD COLUMN kind "
                     "TEXT NOT NULL DEFAULT 'image'")
    if "session_id" not in rcols:
        # anonyme Sitzungskennung (Cookie, keine IP) — haelt Doppelstimmen
        # derselben Sitzung fest und macht sie noetigenfalls ruecknehmbar
        conn.execute("ALTER TABLE ratings ADD COLUMN session_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ratings_session "
        "ON ratings (session_id)")
    # Anonyme Bewertungssitzungen (nur Kennung + Zeitstempel, kein Merkmal).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rating_sessions ("
        " id TEXT PRIMARY KEY,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " last_seen TEXT NOT NULL DEFAULT (datetime('now'))"
        ")")
    # Einmal-Nonces: pro gezogenem Wort genau eine gueltige Bewertung.
    # Verbraucht = Zeile weg (atomar via DELETE).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rating_nonces ("
        " nonce TEXT PRIMARY KEY,"
        " session_id TEXT NOT NULL,"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " expires_at TEXT NOT NULL"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rating_nonces_session "
        "ON rating_nonces (session_id)")
    conn.execute(
        "DELETE FROM rating_nonces WHERE expires_at < datetime('now')")
    conn.execute(
        "DELETE FROM rating_sessions WHERE last_seen < "
        "datetime('now', '-12 hours')")
    # Umzugs-Wuenssche: "Bild passt besser zu einem anderen Wort".
    # Die Pipeline prueft sie (Gemma-Vision) und loest sie auf.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS image_moves ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " filename TEXT NOT NULL,"
        " from_entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " target_word TEXT NOT NULL,"
        " language TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'offen',"
        " note TEXT,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " decided_at TEXT"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_moves_status "
        "ON image_moves (status)")
    # Bestand uebernehmen: vorhandene Hauptbilder in die Galerie eintragen
    conn.execute(
        "INSERT OR IGNORE INTO entry_images (entry_id, filename) "
        "SELECT id, image FROM entries "
        "WHERE image IS NOT NULL AND image != ''")


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
