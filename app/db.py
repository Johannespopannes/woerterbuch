"""Datenbankzugriff für das Wörterbuch. Eine Datei, kein ORM — bewusst simpel.

Zwei Backends über WB_DB (Default: sqlite):
  sqlite  — lokale Datei, Standalone/Tests
  mysql   — MariaDB auf dem Pi (Produktion); parallele Schreiber ohne
            Serialisierung. Verbindung via WB_MYSQL_PW (Unix-Socket).

Das MySQL-Backend stellt eine sqlite-ähnliche Schnittstelle bereit
(execute/fetchone/fetchall/rowcount/lastrowid, dict-Zeilen), damit
main.py unverändert bleibt. SQL-Übersetzung: ? → %s, ULOWER → LOWER
(MariaDB foldet utf8mb4 nativ Unicode), datetime('now') → NOW(),
INSERT OR IGNORE → INSERT IGNORE.
"""
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "woerterbuch.db"
BACKEND = os.environ.get("WB_DB", "sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word        TEXT    NOT NULL,
    language    TEXT    NOT NULL,
    definition  TEXT,
    image       TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    script      TEXT    NOT NULL DEFAULT '',
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
    if "script" not in cols:
        # Schrift der Wortform: '' = Standardschrift der Sprache,
        # 'latn' = Franco-Arabisch (Tunesisch in Lateinschrift) etc.
        conn.execute("ALTER TABLE entries ADD COLUMN script "
                     "TEXT NOT NULL DEFAULT ''")
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
    # Drei-Faelle-Abstimmung statt bloss ja/nein:
    # 'dazu' (Gebrauchswort), 'nicht' (falsche Sprache), 'unsicher'.
    wcols = {r["name"] for r in conn.execute(
        "PRAGMA table_info (word_ratings)")}
    if "kind" not in wcols:
        conn.execute("ALTER TABLE word_ratings ADD COLUMN kind "
                     "TEXT NOT NULL DEFAULT 'dazu'")
        conn.execute("UPDATE word_ratings SET kind = 'dazu' WHERE fits = 1")
        conn.execute("UPDATE word_ratings SET kind = 'nicht' WHERE fits = 0")
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
    # Grammatik-Ebene: pro Lemma (Sprache + Grundform) eine Zeile.
    # Eintraege verweisen ueber ihr bestehendes lemma-Feld darauf.
    # flexion = JSON, z. B. {"cases": {"Singular": {"Nominativ": "der Kanton", ...}}}
    # oder {"tenses": {...}} / {"comparison": {...}} — wird generisch gerendert.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS grammar ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " language TEXT NOT NULL,"
        " lemma TEXT NOT NULL,"
        " word_class TEXT,"
        " gender TEXT,"
        " article TEXT,"
        " flexion TEXT,"
        " source TEXT NOT NULL DEFAULT 'model',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (language, lemma)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_grammar_lemma "
        "ON grammar (language, lemma)")
    # Sprecher-Bewertung (Piper-Stimmen): 👍/👎 pro Sitzung und Stimme,
    # damit sich schlechte Sprecher pro Sprache identifizieren lassen.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS speaker_ratings ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " language TEXT NOT NULL,"
        " voice TEXT NOT NULL,"
        " gut INTEGER NOT NULL,"
        " session_id TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (language, voice, session_id)"
        ")")
    # Gepruefte Externquellen pro Eintrag (Wiktionary/Wikipedia/DWDS).
    # Nur verifizierte Ziele (Existenz per API geprueft) — keine Sackgassen.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_links ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " kind TEXT NOT NULL,"
        " url TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (entry_id, kind)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_links_entry "
        "ON source_links (entry_id)")
    # Sinn-Ebene: ein Wort, mehrere Bedeutungen (Bank = Finanzinstitut
    # vs. Sitzmoebel). Sinne tragen eigene Definitionen, Uebersetzungs-
    # verbindungen zu Woertern ANDERER Sprachen (bedeutungsgerecht!) und
    # belegte Quellen pro Aussage (Wortgeschichte). Der Altbestand auf
    # Wortebene (definitions, Bilder, Bewertungen) bleibt unveraendert.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS senses ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " label TEXT NOT NULL,"
        " word_class TEXT NOT NULL DEFAULT '',"
        " source TEXT NOT NULL DEFAULT 'manual',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_senses_entry ON senses (entry_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sense_definitions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " sense_id INTEGER NOT NULL REFERENCES senses (id),"
        " lang TEXT NOT NULL,"
        " text TEXT NOT NULL,"
        " source TEXT NOT NULL DEFAULT 'model',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (sense_id, lang)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sense_definitions_sense "
        "ON sense_definitions (sense_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sense_translations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " sense_id INTEGER NOT NULL REFERENCES senses (id),"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " approx INTEGER NOT NULL DEFAULT 0,"
        " source TEXT NOT NULL DEFAULT 'model',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (sense_id, entry_id)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sense_translations_sense "
        "ON sense_translations (sense_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sense_translations_entry "
        "ON sense_translations (entry_id)")
    # Herkunfts-/Gebrauchsbelege: pro Sinn, mit der konkreten Aussage,
    # die die Quelle stuetzt (kein bloßer Existenznachweis).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sense_links ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " sense_id INTEGER NOT NULL REFERENCES senses (id),"
        " kind TEXT NOT NULL,"
        " url TEXT NOT NULL,"
        " claim TEXT NOT NULL DEFAULT '',"
        " source TEXT NOT NULL DEFAULT 'manual',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (sense_id, kind, claim)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sense_links_sense "
        "ON sense_links (sense_id)")
    # Bedeutungen in beliebigen Sprachen: pro Eintrag und Definitions-
    # Sprache eine Zeile. lang = Sprache DES Textes ('de' = kanonische
    # deutsche Definition, spiegelt entries.definition; 'it' = italienische
    # Bedeutung eines italienischen Worts usw.).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS definitions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " entry_id INTEGER NOT NULL REFERENCES entries (id),"
        " lang TEXT NOT NULL,"
        " text TEXT NOT NULL,"
        " source TEXT NOT NULL DEFAULT 'model',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " UNIQUE (entry_id, lang)"
        ")")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_definitions_entry "
        "ON definitions (entry_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_definitions_lang "
        "ON definitions (lang)")
    # Bestand uebernehmen: entries.definition ist die deutsche Definition
    conn.execute(
        "INSERT OR IGNORE INTO definitions (entry_id, lang, text, source) "
        "SELECT id, 'de', definition, 'legacy' FROM entries "
        "WHERE definition IS NOT NULL AND definition != ''")
    # Sinn-Ebene nachruesten (Spalten fuer bestehende Tabellen)
    sc = {r["name"] for r in conn.execute("PRAGMA table_info (senses)")}
    if "word_class" not in sc:
        conn.execute("ALTER TABLE senses ADD COLUMN word_class TEXT NOT NULL DEFAULT ''")
    tc = {r["name"] for r in conn.execute("PRAGMA table_info (sense_translations)")}
    if "approx" not in tc:
        conn.execute("ALTER TABLE sense_translations ADD COLUMN approx INTEGER NOT NULL DEFAULT 0")


# ---------- MariaDB-Backend (Produktion) ----------

SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS entries (
    id INT PRIMARY KEY AUTO_INCREMENT,
    word TEXT COLLATE utf8mb4_bin NOT NULL,
    language TEXT NOT NULL,
    definition TEXT,
    image TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (NOW()),
    lemma TEXT,
    form TEXT,
    confidence REAL,
    evidence TEXT,
    sprache_unsicher INT NOT NULL DEFAULT 0,
    script TEXT NOT NULL DEFAULT '',
    UNIQUE (word, language)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_entries_prefix ON entries (language(8), word(191));
CREATE TABLE IF NOT EXISTS ratings (
    id INT PRIMARY KEY AUTO_INCREMENT,
    entry_id INT NOT NULL REFERENCES entries (id),
    understood INT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    image TEXT,
    kind TEXT NOT NULL DEFAULT 'image',
    session_id TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_ratings_entry ON ratings (entry_id);
CREATE INDEX IF NOT EXISTS idx_ratings_session ON ratings (session_id(64));
CREATE TABLE IF NOT EXISTS word_ratings (
    id INT PRIMARY KEY AUTO_INCREMENT,
    entry_id INT NOT NULL REFERENCES entries (id),
    fits INT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    kind TEXT NOT NULL DEFAULT 'dazu'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_word_ratings_entry ON word_ratings (entry_id);
CREATE TABLE IF NOT EXISTS entry_images (
    id INT PRIMARY KEY AUTO_INCREMENT,
    entry_id INT NOT NULL REFERENCES entries (id),
    filename TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (entry_id, filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_entry_images_entry ON entry_images (entry_id);
CREATE TABLE IF NOT EXISTS search_stats (
    term TEXT COLLATE utf8mb4_bin NOT NULL,
    language TEXT NOT NULL,
    count INT NOT NULL DEFAULT 0,
    UNIQUE (term, language)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS rating_sessions (
    id VARCHAR(64) PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    last_seen TEXT NOT NULL DEFAULT (NOW())
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS rating_nonces (
    nonce VARCHAR(128) PRIMARY KEY,
    session_id TEXT NOT NULL,
    entry_id INT NOT NULL REFERENCES entries (id),
    created_at TEXT NOT NULL DEFAULT (NOW()),
    expires_at TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_rating_nonces_session ON rating_nonces (session_id(64));
CREATE TABLE IF NOT EXISTS image_moves (
    id INT PRIMARY KEY AUTO_INCREMENT,
    filename TEXT NOT NULL,
    from_entry_id INT NOT NULL REFERENCES entries (id),
    target_word TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offen',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    decided_at TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_image_moves_status ON image_moves (status);
CREATE TABLE IF NOT EXISTS grammar (
    id INT PRIMARY KEY AUTO_INCREMENT,
    language TEXT NOT NULL,
    lemma TEXT COLLATE utf8mb4_bin NOT NULL,
    word_class TEXT,
    gender TEXT,
    article TEXT,
    flexion TEXT,
    source TEXT NOT NULL DEFAULT 'model',
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (language, lemma)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_grammar_lemma ON grammar (language(8), lemma(191));
CREATE TABLE IF NOT EXISTS speaker_ratings (
    id INT PRIMARY KEY AUTO_INCREMENT,
    language TEXT NOT NULL,
    voice TEXT NOT NULL,
    gut INT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (language(8), voice(64), session_id(64))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_speaker_ratings_voice ON speaker_ratings (language(8), voice(64));
CREATE TABLE IF NOT EXISTS definitions (
    id INT PRIMARY KEY AUTO_INCREMENT,
    entry_id INT NOT NULL REFERENCES entries (id),
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'model',
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (entry_id, lang(8))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_definitions_entry ON definitions (entry_id);
CREATE INDEX IF NOT EXISTS idx_definitions_lang ON definitions (lang(8));
CREATE TABLE IF NOT EXISTS source_links (
    id INT PRIMARY KEY AUTO_INCREMENT,
    entry_id INT NOT NULL REFERENCES entries (id),
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (entry_id, kind(16))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_source_links_entry ON source_links (entry_id);
CREATE TABLE IF NOT EXISTS senses (
    id INT PRIMARY KEY AUTO_INCREMENT,
    entry_id INT NOT NULL REFERENCES entries (id),
    label TEXT NOT NULL,
    word_class TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (NOW())
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_senses_entry ON senses (entry_id);
CREATE TABLE IF NOT EXISTS sense_definitions (
    id INT PRIMARY KEY AUTO_INCREMENT,
    sense_id INT NOT NULL REFERENCES senses (id),
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'model',
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (sense_id, lang(8))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_sense_definitions_sense ON sense_definitions (sense_id);
CREATE TABLE IF NOT EXISTS sense_translations (
    id INT PRIMARY KEY AUTO_INCREMENT,
    sense_id INT NOT NULL REFERENCES senses (id),
    entry_id INT NOT NULL REFERENCES entries (id),
    approx INT NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'model',
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (sense_id, entry_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_sense_translations_sense ON sense_translations (sense_id);
CREATE INDEX IF NOT EXISTS idx_sense_translations_entry ON sense_translations (entry_id);
CREATE TABLE IF NOT EXISTS sense_links (
    id INT PRIMARY KEY AUTO_INCREMENT,
    sense_id INT NOT NULL REFERENCES senses (id),
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    claim VARCHAR(191) NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (NOW()),
    UNIQUE (sense_id, kind(16), claim)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX IF NOT EXISTS idx_sense_links_sense ON sense_links (sense_id);
"""

_TRANSLATE = [
    (re.compile(r"INSERT OR IGNORE", re.I), "INSERT IGNORE"),
    (re.compile(r"INSERT OR REPLACE", re.I), "REPLACE"),
    (re.compile(r"ON CONFLICT\s*\([^)]*\)\s*DO UPDATE\s+SET",
                re.I), "ON DUPLICATE KEY UPDATE"),
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
    """Aktueller Zeitstempel im DB-Stringformat, backend-konsistent.

    SQLite datetime('now') ist UTC, MariaDB NOW() Lokalzeit — das hier
    liefert jeweils dasselbe Format/Zeitbasis wie die SQL-Pendants, damit
    String-Vergleiche mit bestehenden created_at/last_seen-Zeilen stimmen.
    """
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


def get_conn():
    """Backend-Auswahl: WB_DB=mysql → MariaDB, sonst SQLite."""
    if BACKEND == "mysql":
        return MyConn()
    return _get_sqlite_conn()


def _get_sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    # Die Migrations-DML (Nonces-/Sessions-Bereinigung, Galerie-Sync)
    # sofort festschreiben — sonst haelt JEDE Connection eine offene
    # Transaktion, und parallele Connections blockieren sich gegenseitig
    # (database is locked).
    conn.commit()
    # SQLites LOWER()/LIKE-Fold ist nur ASCII — für Umlaute (Ö vs. ö)
    # eine echte Unicode-Variante aus Python registrieren.
    conn.create_function(
        "ULOWER", 1, lambda s: s.lower() if isinstance(s, str) else s
    )
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}
