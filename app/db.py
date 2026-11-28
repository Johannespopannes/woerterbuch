"""Datenbankzugriff für das Wörterbuch — Fachmodell v2 (docs/fachmodell.md).

Eine Datei, kein ORM — bewusst simpel. Zwei Backends über WB_DB
(Default: sqlite):
  sqlite  — lokale Datei, Standalone/Tests
  mysql   — MariaDB auf dem Pi (Produktion)

Das MySQL-Backend stellt eine sqlite-ähnliche Schnittstelle bereit
(execute/fetchone/fetchall/rowcount/lastrowid, dict-Zeilen), damit
derselbe Schema- und Migrationscode auf beiden Backends läuft — das
sind die reproduzierbaren MariaDB-Migrationen. SQL-Übersetzung:
? → %s, AUTOINCREMENT → AUTO_INCREMENT, ULOWER → LOWER (MariaDB foldet
utf8mb4 nativ Unicode), datetime('now') → NOW(),
INSERT OR IGNORE → INSERT IGNORE, INSERT OR REPLACE → REPLACE,
RANDOM( → RAND(, ON CONFLICT .. DO UPDATE → ON DUPLICATE KEY UPDATE,
CREATE INDEX IF NOT EXISTS → CREATE INDEX (MariaDB kennt es nicht),
COLLATE BINARY → COLLATE utf8mb4_bin (Wortformen behalten exakt die
utf8mb4_bin-Semantik der Alttabelle, inkl. Groß-/Kleinschreibung).

Typregel: indizierte Spalten sind VARCHAR(n) (MariaDB indiziert TEXT
nur mit Präfixlänge), Inhaltsfelder bleiben TEXT. Gemessene Maxima
sind dokumentiert in tools/migrate_data.py.

Fachmodell (verbindlich, docs/fachmodell.md):
  Wortform  = Schreibweise (keine Grammatik/Semantik)
  Lexem     = grammatische Identität (eigene ID, Wortart, Merkmale,
              Paradigma, Etymologie, Evidenz)
  Bedeutung = Definition, Beispiele, Synonyme, Bilder, Belege
  Lesart    = (Wortform, Lexem), trägt Aussprachen; Audio je
              (Lesart, Aussprache, Sprecher)
  Übersetzung = Beziehung Bedeutung ↔ Bedeutung (keine Obergrenze)

Unzugeordneter Altbestand (Wortdefinitionen, Wortbilder, Wort-
beispiele) bleibt als solcher erhalten: lexeme_defs / lexeme_images /
lexeme_examples — die Pipeline ordnet zu, Unsicherheit bleibt als
Daten erhalten (status='provisional').
"""
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "woerterbuch.db"
BACKEND = os.environ.get("WB_DB", "sqlite")

# ---------------------------------------------------------------------------
# Schema v2. Änderungen NUR hier + Migrations-Schritt in _migrate() eintragen
# (name in schema_migrations, idempotent, läuft auf sqlite UND MariaDB).
# ---------------------------------------------------------------------------

_SCHEMA_STEPS: list[tuple[str, str]] = [
    ("v2_basis", """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name VARCHAR(191) PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS word_forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language VARCHAR(10) NOT NULL,
    word VARCHAR(512) NOT NULL COLLATE BINARY,
    script VARCHAR(40) NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (language, word, script)
);
CREATE INDEX IF NOT EXISTS idx_forms_prefix ON word_forms (language, word);
CREATE TABLE IF NOT EXISTS lexemes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language VARCHAR(10) NOT NULL,
    lemma VARCHAR(512) NOT NULL,
    word_class VARCHAR(64) NOT NULL DEFAULT '',
    attrs TEXT NOT NULL DEFAULT '',
    evidence TEXT,
    confidence REAL,
    sprache_unsicher INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'provisional',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lexemes_lemma ON lexemes (language, lemma);
CREATE TABLE IF NOT EXISTS lexeme_forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    form_id INTEGER NOT NULL REFERENCES word_forms (id),
    cell VARCHAR(191) NOT NULL DEFAULT '',
    UNIQUE (lexeme_id, form_id, cell)
);
CREATE INDEX IF NOT EXISTS idx_lexeme_forms_lexeme ON lexeme_forms (lexeme_id);
CREATE INDEX IF NOT EXISTS idx_lexeme_forms_form ON lexeme_forms (form_id);
CREATE TABLE IF NOT EXISTS meanings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    label TEXT NOT NULL DEFAULT '',
    word_class VARCHAR(64) NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'provisional',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_meanings_lexeme ON meanings (lexeme_id);
CREATE TABLE IF NOT EXISTS meaning_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    lang VARCHAR(10) NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (meaning_id, lang)
);
CREATE INDEX IF NOT EXISTS idx_meaning_defs ON meaning_definitions (meaning_id);
CREATE TABLE IF NOT EXISTS lexeme_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    lang VARCHAR(10) NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lexeme_id, lang)
);
CREATE INDEX IF NOT EXISTS idx_lexeme_defs ON lexeme_defs (lexeme_id);
CREATE INDEX IF NOT EXISTS idx_lexeme_defs_lang ON lexeme_defs (lang);
CREATE TABLE IF NOT EXISTS lexeme_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    form_id INTEGER REFERENCES word_forms (id),
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lexeme_examples ON lexeme_examples (lexeme_id);
CREATE TABLE IF NOT EXISTS lexeme_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    filename VARCHAR(512) NOT NULL,
    is_main INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lexeme_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_lexeme_images ON lexeme_images (lexeme_id);
CREATE TABLE IF NOT EXISTS meaning_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    filename VARCHAR(512) NOT NULL,
    is_main INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (meaning_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_meaning_images ON meaning_images (meaning_id);
CREATE TABLE IF NOT EXISTS meaning_translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    to_meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    approx INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (from_meaning_id, to_meaning_id)
);
CREATE INDEX IF NOT EXISTS idx_meaning_trans_from ON meaning_translations (from_meaning_id);
CREATE INDEX IF NOT EXISTS idx_meaning_trans_to ON meaning_translations (to_meaning_id);
CREATE TABLE IF NOT EXISTS meaning_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    form_id INTEGER REFERENCES word_forms (id),
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_meaning_examples ON meaning_examples (meaning_id);
CREATE TABLE IF NOT EXISTS meaning_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    kind VARCHAR(64) NOT NULL,
    url TEXT NOT NULL,
    claim VARCHAR(191) NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (meaning_id, kind, claim)
);
CREATE INDEX IF NOT EXISTS idx_meaning_links ON meaning_links (meaning_id);
CREATE TABLE IF NOT EXISTS meaning_synonyms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    a_meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    b_meaning_id INTEGER NOT NULL REFERENCES meanings (id),
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (a_meaning_id, b_meaning_id)
);
CREATE INDEX IF NOT EXISTS idx_meaning_syn_a ON meaning_synonyms (a_meaning_id);
CREATE INDEX IF NOT EXISTS idx_meaning_syn_b ON meaning_synonyms (b_meaning_id);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL REFERENCES word_forms (id),
    lexeme_id INTEGER REFERENCES lexemes (id),
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (form_id, lexeme_id)
);
CREATE INDEX IF NOT EXISTS idx_readings_form ON readings (form_id);
CREATE INDEX IF NOT EXISTS idx_readings_lexeme ON readings (lexeme_id);
CREATE TABLE IF NOT EXISTS pronunciations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings (id),
    transcription VARCHAR(512) NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (reading_id, transcription)
);
CREATE INDEX IF NOT EXISTS idx_pronunciations ON pronunciations (reading_id);
CREATE TABLE IF NOT EXISTS reading_audio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings (id),
    voice VARCHAR(64) NOT NULL,
    length_scale REAL NOT NULL DEFAULT 1.0,
    filename VARCHAR(512) NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (reading_id, voice, length_scale)
);
CREATE INDEX IF NOT EXISTS idx_reading_audio ON reading_audio (reading_id);
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    understood INTEGER NOT NULL,
    image TEXT NOT NULL DEFAULT '',
    kind VARCHAR(32) NOT NULL DEFAULT 'image',
    session_id VARCHAR(64),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ratings_lexeme ON ratings (lexeme_id);
CREATE INDEX IF NOT EXISTS idx_ratings_session ON ratings (session_id);
CREATE TABLE IF NOT EXISTS lexeme_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    kind VARCHAR(32) NOT NULL DEFAULT 'dazu',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lexeme_ratings ON lexeme_ratings (lexeme_id);
CREATE TABLE IF NOT EXISTS source_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    kind VARCHAR(16) NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lexeme_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_source_links ON source_links (lexeme_id);
CREATE TABLE IF NOT EXISTS search_stats (
    term VARCHAR(512) NOT NULL,
    language VARCHAR(10) NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (term, language)
);
CREATE TABLE IF NOT EXISTS rating_sessions (
    id VARCHAR(64) PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS rating_nonces (
    nonce VARCHAR(128) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rating_nonces_session ON rating_nonces (session_id);
CREATE TABLE IF NOT EXISTS image_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename VARCHAR(512) NOT NULL,
    from_lexeme_id INTEGER NOT NULL REFERENCES lexemes (id),
    target_word VARCHAR(512) NOT NULL,
    language VARCHAR(10) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'offen',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_image_moves_status ON image_moves (status);
CREATE TABLE IF NOT EXISTS speaker_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language VARCHAR(10) NOT NULL,
    voice VARCHAR(64) NOT NULL,
    gut INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (language, voice, session_id)
);
"""),
    ("v2_head_form_links", """
INSERT OR IGNORE INTO lexeme_forms (lexeme_id, form_id, cell)
SELECT lexeme_id, form_id, '' FROM readings
WHERE lexeme_id IS NOT NULL;
"""),
    ("v2_unsubstantiated_candidates", """
UPDATE lexemes SET status = 'candidate'
WHERE status = 'provisional'
  AND (word_class = '' OR word_class IS NULL)
  AND (evidence IS NULL OR evidence = '')
  AND NOT EXISTS (
    SELECT 1 FROM lexeme_defs d WHERE d.lexeme_id = lexemes.id)
  AND NOT EXISTS (
    SELECT 1 FROM meanings m WHERE m.lexeme_id = lexemes.id)
  AND NOT EXISTS (
    SELECT 1 FROM lexeme_images i WHERE i.lexeme_id = lexemes.id)
  AND NOT EXISTS (
    SELECT 1 FROM ratings r WHERE r.lexeme_id = lexemes.id)
  AND NOT EXISTS (
    SELECT 1 FROM lexeme_ratings r WHERE r.lexeme_id = lexemes.id);
"""),
    ("v2_unassigned_legacy_records", """
CREATE TABLE IF NOT EXISTS unassigned_legacy_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type VARCHAR(40) NOT NULL,
    legacy_table VARCHAR(80) NOT NULL,
    legacy_id INTEGER NOT NULL,
    related_id INTEGER,
    payload TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (legacy_table, legacy_id)
);
CREATE INDEX IF NOT EXISTS idx_unassigned_legacy_type
ON unassigned_legacy_records (record_type);
"""),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotente Schema-Schritte, backend-übergreifend (sqlite + MariaDB).

    Jeder Schritt steht einmal in schema_migrations; bereits angewendete
    Schritte werden übersprungen — dasselbe Skript läuft daher auf einer
    leeren Test-DB wie auf der Produktions-MariaDB.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name VARCHAR(191) PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    done = {r["name"] for r in conn.execute(
        "SELECT name FROM schema_migrations").fetchall()}
    for name, ddl in _SCHEMA_STEPS:
        if name in done:
            continue
        conn.executescript(ddl)
        conn.execute(
            "INSERT INTO schema_migrations (name) VALUES (?)", (name,))
        conn.commit()


# ---------- MariaDB-Backend (Produktion) ----------

_TRANSLATE = [
    (re.compile(r"INSERT OR IGNORE", re.I), "INSERT IGNORE"),
    (re.compile(r"INSERT OR REPLACE", re.I), "REPLACE"),
    (re.compile(r"ON CONFLICT\s*\([^)]*\)\s*DO UPDATE\s+SET",
                re.I), "ON DUPLICATE KEY UPDATE"),
    (re.compile(r"CREATE INDEX IF NOT EXISTS", re.I), "CREATE INDEX"),
    (re.compile(r"\bAUTOINCREMENT\b", re.I), "AUTO_INCREMENT"),
    (re.compile(r"COLLATE BINARY", re.I), "COLLATE utf8mb4_bin"),
    (re.compile(r"ULOWER", re.I), "LOWER"),
    (re.compile(r"\bRANDOM\s*\(", re.I), "RAND("),
    (re.compile(r"datetime\('now'\)"), "NOW()"),
    (re.compile(r"\?"), "%s"),
]


def _translate(sql: str) -> str:
    for pat, rep in _TRANSLATE:
        sql = pat.sub(rep, sql)
    return sql


def now_str(minutes: int = 0) -> str:
    """Aktueller Zeitstempel im DB-Stringformat, backend-konsistent."""
    from datetime import datetime, timedelta, timezone
    if BACKEND == "mysql":
        dt = datetime.now() + timedelta(minutes=minutes)
    else:
        dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class _MyCursor:
    """sqlite-Cursor-Emulation über PyMySQL (Dict-Zeilen)."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def fetchone(self) -> dict | None:
        return self._cur.fetchone()

    def fetchall(self) -> list[dict]:
        return list(self._cur.fetchall())

    @property
    def rowcount(self) -> int:
        return max(self._cur.rowcount, 0)

    @property
    def lastrowid(self) -> int:
        return self._cur.lastrowid


class MyConn:
    """sqlite-Connection-Emulation über PyMySQL (Unix-Socket)."""

    def __init__(self) -> None:
        import pymysql  # lazy: sqlite-Nutzer brauchen es nicht
        kwargs: dict[str, Any] = dict(
            user=os.environ.get("WB_MYSQL_USER", "woerterbuch"),
            password=os.environ["WB_MYSQL_PW"],
            database=os.environ.get("WB_MYSQL_DB", "woerterbuch"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
        host = os.environ.get("WB_MYSQL_HOST")
        if host:
            # Fernzugriff (z. B. Test via SSH-Tunnel)
            kwargs.update(host=host,
                          port=int(os.environ.get("WB_MYSQL_PORT", "3306")))
        else:
            kwargs.update(unix_socket="/run/mysqld/mysqld.sock")
        self._conn = pymysql.connect(**kwargs)

    def execute(self, sql: str, params: Any = ()) -> _MyCursor:
        cur = self._conn.cursor()
        cur.execute(_translate(sql), params or ())
        return _MyCursor(cur)

    def executescript(self, script: str) -> None:
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                self.execute(stmt)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


_mysql_migration_done = False
_mysql_migration_lock = threading.Lock()


def get_conn():
    """Backend-Auswahl: WB_DB=mysql → MariaDB, sonst SQLite."""
    if BACKEND == "mysql":
        global _mysql_migration_done
        conn = MyConn()
        if not _mysql_migration_done:
            with _mysql_migration_lock:
                if not _mysql_migration_done:
                    _migrate(conn)
                    _mysql_migration_done = True
        return conn
    return _get_sqlite_conn()


def _get_sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    conn.commit()
    # SQLites LOWER()/LIKE-Fold ist nur ASCII — für Umlaute (Ö vs. ö)
    # eine echte Unicode-Variante aus Python registrieren.
    conn.create_function(
        "ULOWER", 1, lambda s: s.lower() if isinstance(s, str) else s
    )
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}
