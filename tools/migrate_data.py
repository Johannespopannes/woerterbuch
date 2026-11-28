# -*- coding: utf-8 -*-
"""Einmal-Migration Altbestand → Fachmodell v2 (docs/fachmodell.md).

Läuft auf dem Pi gegen die Produktions-MariaDB (Unix-Socket):

    WB_MYSQL_PW=<db-passwort> python3 tools/migrate_data.py

Testlauf gegen eine Kopie (z. B. woerterbuch_test):

    WB_MYSQL_DB=woerterbuch_test WB_MYSQL_PW=... python3 tools/migrate_data.py

Prinzipien (verbindlich):
- verlustfrei: alte Tabellen werden nach migration_legacy_* UMBENANNT
  (erst nach erfolgreicher Verifikation + Laufzeit der Neuimplementierung
  wird ein separates Aufräumskript sie entfernen);
- keine erfundenen sprachlichen Zuordnungen: Wortebenen-Informationen
  landen als unzugeordneter Bestand (lexeme_defs / lexeme_images /
  lexeme_examples), status='provisional' markiert den unbestätigten
  Altbestand;
- ID-erhaltend: word_forms.id = entries.id, lexemes.id = entries.id,
  meanings.id = senses.id — Fremdschlüssel bleiben stabil;
- nicht eindeutig Zuordenbares (z. B. Übersetzung auf Eintrag mit
  mehreren Bedeutungen) wird übersprungen und verbleibt im Legacy-
  Bestand — keine Erfindung von Zuordnungen.

Runbook (Produktion):
  1. systemctl stop woerterbuch-pipeline woerterbuch
  2. Backup:  mysqldump woerterbuch > /mnt/disk2/backups/woerterbuch-v1-<datum>.sql
  3. WB_MYSQL_PW=... python3 tools/migrate_data.py
  4. Code deployen (git push forgejo main → Runner), Dienste starten
  5. Verifizieren; Aufräumen erst danach: tools/drop_legacy.py
"""
import importlib.util
import json
import os
import re
import sys

import pymysql

DB_NAME = os.environ.get("WB_MYSQL_DB", "woerterbuch")
AUDIO_DIR = os.environ.get(
    "WB_AUDIO_DIR", "/home/johannes/woerterbuch/audio")

conn = pymysql.connect(user=os.environ.get("WB_MYSQL_USER", "woerterbuch"),
                       password=os.environ["WB_MYSQL_PW"],
                       database=DB_NAME,
                       charset="utf8mb4",
                       unix_socket="/run/mysqld/mysqld.sock",
                       cursorclass=pymysql.cursors.DictCursor,
                       autocommit=False)
cur = conn.cursor()


def log(msg: str) -> None:
    print(msg, flush=True)


def run(sql: str, params=None) -> None:
    cur.execute(sql, params or ())


def q(sql: str, params=None) -> list:
    run(sql, params)
    return list(cur.fetchall())


def one(sql: str, params=None):
    run(sql, params)
    return cur.fetchone()


def executemany(sql: str, rows, chunk: int = 2000) -> None:
    """In Chunks — ein 59k-Zeilen-Multi-VALUES-Statement kann sonst
    max_allowed_packet sprengen (Evidenz-Texte sind lang)."""
    rows = list(rows)
    for i in range(0, len(rows), chunk):
        cur.executemany(sql, rows[i:i + chunk])


def table_exists(name: str) -> bool:
    return one("SELECT 1 FROM information_schema.TABLES"
               " WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
               (DB_NAME, name)) is not None


if table_exists("migration_legacy_entries"):
    log("!! Altbestand bereits nach migration_legacy_* umbenannt — "
        "Abbruch (Idempotenz).")
    sys.exit(0)

# --- Kollidierende Alt-Tabellen ZUERST umbenennen --------------------------
# (vor dem Schema-Aufbau: CREATE TABLE IF NOT EXISTS sähe die Alttabellen
#  und überspräfe das Anlegen; danach bräche jeder Index auf den neuen
#  Spalten. MySQL-DDL committed implizit.)
log("== 0/10 Namenskollisionen auflösen ==")
for alt in ("ratings", "source_links", "search_stats", "rating_sessions",
            "rating_nonces", "speaker_ratings", "image_moves"):
    if table_exists(alt):
        short = alt[len("rating_"):] if alt.startswith("rating_") else alt
        run(f"RENAME TABLE {alt} TO migration_legacy_rating_{short}"
            if alt.startswith("rating_")
            else f"RENAME TABLE {alt} TO migration_legacy_{alt}")
log("   umbenannt")

log(f"== Zieldatenbank: {DB_NAME} ==")
log("== Schema v2 anlegen (derselbe Code wie die Anwendung) ==")
os.environ["WB_DB"] = "mysql"
spec = importlib.util.spec_from_file_location(
    "wbd", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "app", "db.py"))
wbd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wbd)


class _Conn:
    """Minimaler Adapter, damit wbd._migrate unseren Cursor nutzt
    (inkl. derselben SQL-Übersetzung wie das MySQL-Backend)."""

    def execute(self, sql, params=()):
        run(wbd._translate(sql), params)
        return cur

    def executescript(self, script):
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                run(wbd._translate(stmt))

    def commit(self):
        conn.commit()


wbd._migrate(_Conn())
log("   Schema-Schritte: "
    + ", ".join(r["name"] for r in q("SELECT name FROM schema_migrations")))

log("== 1/10 Wortformen ==")
run("INSERT INTO word_forms (id, language, word, script, source,"
    " created_at) SELECT id, language, word, script, source, created_at"
    " FROM entries")
log(f"   {cur.rowcount} Wortformen")

log("== 2/10 Lexeme (provisional, 1:1-Träger, keine Legitimation) ==")
gram = {(g["language"], g["lemma"]): g for g in q(
    "SELECT language, lemma, word_class, gender, article, flexion, source"
    " FROM grammar")}
entries = q("SELECT id, language, word, lemma, form, evidence, confidence,"
            " sprache_unsicher, source, created_at FROM entries")
lex_rows, flex_raw = [], []
for e in entries:
    lemma = e["lemma"] or e["word"]
    g = gram.get((e["language"], lemma)) or {}
    attrs = {}
    if e.get("form"):
        attrs["form"] = e["form"]  # Legacy-Qualifizierer (z. B. Grundform)
    if g.get("gender"):
        attrs["genus"] = g["gender"]
    if g.get("article"):
        attrs["article"] = g["article"]
    if g.get("source"):
        attrs["grammar_source"] = g["source"]
    flexion = g.get("flexion")
    if flexion:
        try:
            json.loads(flexion)
        except Exception:
            attrs["flexion_raw"] = flexion
            flexion = None
    lex_rows.append((e["id"], e["language"], lemma,
                     g.get("word_class") or "",
                     json.dumps(attrs, ensure_ascii=False) if attrs else "",
                     e["evidence"], e["confidence"],
                     e["sprache_unsicher"] or 0, "provisional",
                     e["source"], e["created_at"]))
    if flexion:
        flex_raw.append((e["id"], e["language"], flexion))
executemany("INSERT INTO lexemes (id, language, lemma, word_class, attrs,"
            " evidence, confidence, sprache_unsicher, status, source,"
            " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            lex_rows)
log(f"   {len(lex_rows)} Lexeme (provisional)"
    f", {sum(1 for r in flex_raw)} mit Paradigma")

log("== 3/10 Flexionsparadigmen ==")
forms = {(r["language"], r["word"]): r["id"]
         for r in q("SELECT id, language, word FROM word_forms")}
lf_rows, new_forms = [], {}


def cell_walk(node, path, lex_id, language):
    if isinstance(node, dict):
        for k, v in node.items():
            cell_walk(v, path + [str(k)], lex_id, language)
    elif isinstance(node, str) and node.strip():
        key = (language, node)
        if key not in forms:
            if key not in new_forms:
                run("INSERT INTO word_forms (language, word, script, source)"
                    " VALUES (%s,%s,'','paradigm')", (language, node))
                new_forms[key] = cur.lastrowid
            fid = new_forms[key]
        else:
            fid = forms[key]
        lf_rows.append((lex_id, fid,
                        json.dumps(path, ensure_ascii=False)))


for lex_id, language, flexion in flex_raw:
    try:
        cell_walk(json.loads(flexion), [], lex_id, language)
    except Exception as ex:
        log(f"   ! Flexion Lexem {lex_id} unlesbar: {ex}")
executemany("INSERT IGNORE INTO lexeme_forms (lexeme_id, form_id, cell)"
            " VALUES (%s,%s,%s)", lf_rows)
log(f"   {len(lf_rows)} Paradigmazellen,"
    f" {len(new_forms)} neue Wortformen (belegte Zeichenfolgen)")

log("== 4/10 Bedeutungen + Sinnbeschreibungen ==")
run("INSERT INTO meanings (id, lexeme_id, label, word_class, status,"
    " source, created_at) SELECT id, entry_id, label, word_class,"
    " 'provisional', source, created_at FROM senses")
log(f"   {cur.rowcount} Bedeutungen")
run("INSERT INTO meaning_definitions (meaning_id, lang, text, source,"
    " created_at) SELECT sense_id, lang, text, source, created_at"
    " FROM sense_definitions")
log(f"   {cur.rowcount} Sinnbeschreibungen")

log("== 5/10 Wortebenen-Definitionen (unzugeordneter Bestand) ==")
run("INSERT INTO lexeme_defs (lexeme_id, lang, text, source, created_at)"
    " SELECT entry_id, lang, text, source, created_at FROM definitions")
n_defs = cur.rowcount
orphan = q("SELECT e.id, e.definition FROM entries e"
           " WHERE e.definition IS NOT NULL AND e.definition != ''"
           " AND NOT EXISTS (SELECT 1 FROM definitions d"
           " WHERE d.entry_id = e.id AND d.lang = 'de'"
           " AND d.text = e.definition)")
executemany("INSERT IGNORE INTO lexeme_defs (lexeme_id, lang, text,"
            " source) VALUES (%s,'de',%s,'legacy-entry')",
            [(r["id"], r["definition"]) for r in orphan])
log(f"   {n_defs} Definitionen + {len(orphan)} Einzel-Reste aus"
    " entries.definition")

log("== 6/10 Bilder (Wortebene, unzugeordnet) ==")
li = [(r["entry_id"], r["filename"], r["main"]) for r in q(
    "SELECT ei.entry_id, ei.filename, (e.image = ei.filename) AS main"
    " FROM entry_images ei JOIN entries e ON e.id = ei.entry_id")]
# Hauptbilder, die gar keine Galerie-Zeile haben (58 im Altbestand):
li += [(r["id"], r["image"], 1) for r in q(
    "SELECT id, image FROM entries"
    " WHERE image IS NOT NULL AND image != ''"
    " AND NOT EXISTS (SELECT 1 FROM entry_images ei"
    " WHERE ei.entry_id = entries.id AND ei.filename = entries.image)")]
executemany("INSERT INTO lexeme_images (lexeme_id, filename, is_main)"
            " VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE is_main = VALUES(is_main)", li)
log(f"   {len(li)} Bilder")

log("== 7/10 Übersetzungen, Belege, Beispiele ==")
mt, skipped_t = [], 0
for t in q("SELECT sense_id, entry_id, approx, source, created_at"
           " FROM sense_translations"):
    ms = q("SELECT id FROM meanings WHERE lexeme_id = %s"
           " ORDER BY id LIMIT 2", (t["entry_id"],))
    if len(ms) == 1:
        mt.append((t["sense_id"], ms[0]["id"], t["approx"],
                   t["source"], t["created_at"]))
    else:
        skipped_t += 1
executemany("INSERT INTO meaning_translations (from_meaning_id,"
            " to_meaning_id, approx, source, created_at)"
            " VALUES (%s,%s,%s,%s,%s)", mt)
log(f"   {len(mt)} Übersetzungsbeziehungen"
    f" ({skipped_t} ohne eindeutige Zielbedeutung übersprungen —"
    " bleiben im Legacy-Bestand, keine Erfindung)")
run("INSERT INTO meaning_links (meaning_id, kind, url, claim, source,"
    " created_at) SELECT sense_id, kind, url, claim, source, created_at"
    " FROM sense_links")
log(f"   {cur.rowcount} Belege")
# Beispielsätze waren Eintrags-/Wortebenen-Information; ohne belegte
# Bedeutungszuordnung → unzugeordneter Bestand in lexeme_examples
# (auch bei Einträgen MIT Sinnen: Zuordnung zur konkreten Bedeutung
# wäre eine erfundene Zuordnung).
ex_rows = []
for e in q("SELECT id, evidence FROM entries"
           " WHERE evidence LIKE %s", ("%beispielsaetze%",)):
    try:
        ev = json.loads(e["evidence"])
    except Exception:
        continue
    for s in (ev.get("beispielsaetze") or [])[:8]:
        if isinstance(s, str) and s.strip():
            ex_rows.append((e["id"], e["id"], s))
executemany("INSERT INTO lexeme_examples (lexeme_id, form_id, text,"
            " source) VALUES (%s,%s,%s,'legacy')", ex_rows)
log(f"   {len(ex_rows)} Beispielsätze (unzugeordneter Bestand)")

log("== 8/10 Stimmen, Kuratierung, Nonces, Moves, Links ==")
# Verwaiste Zeilen (Bewertungen für früher gelöschte Einträge) werden
# gezählt, übersprungen und im Abschlussbericht sichtbar — sie sind im
# Altbestand (migration_legacy_*) weiterhin vorhanden.
orph = one("SELECT COUNT(*) c FROM migration_legacy_ratings r"
           " LEFT JOIN lexemes l ON l.id = r.entry_id WHERE l.id IS NULL")["c"]
run("INSERT INTO ratings (lexeme_id, understood, image, kind, session_id,"
    " created_at) SELECT r.entry_id, r.understood, COALESCE(r.image,''),"
    " r.kind, r.session_id, r.created_at"
    " FROM migration_legacy_ratings r"
    " JOIN lexemes l ON l.id = r.entry_id")
log(f"   {cur.rowcount} Bild-/unknown-Stimmen"
    f" ({orph} verwaist übersprungen — Eintrag existiert nicht mehr)")
orph = one("SELECT COUNT(*) c FROM word_ratings r"
           " LEFT JOIN lexemes l ON l.id = r.entry_id WHERE l.id IS NULL")["c"]
run("INSERT INTO lexeme_ratings (lexeme_id, kind, created_at)"
    " SELECT r.entry_id,"
    " CASE WHEN r.fits=1 THEN 'dazu' ELSE 'nicht' END, r.created_at"
    " FROM word_ratings r JOIN lexemes l ON l.id = r.entry_id")
log(f"   {cur.rowcount} Kuratier-Stimmen (fits→kind, {orph} verwaist"
    " übersprungen)")
orph = one("SELECT COUNT(*) c FROM migration_legacy_source_links r"
           " LEFT JOIN lexemes l ON l.id = r.entry_id WHERE l.id IS NULL")["c"]
run("INSERT INTO source_links (lexeme_id, kind, url, created_at)"
    " SELECT r.entry_id, r.kind, r.url, r.created_at"
    " FROM migration_legacy_source_links r"
    " JOIN lexemes l ON l.id = r.entry_id")
log(f"   {cur.rowcount} geprüfte Externlinks ({orph} verwaist übersprungen)")
run("INSERT INTO search_stats (term, language, count)"
    " SELECT term, language, count FROM migration_legacy_search_stats")
run("INSERT INTO rating_sessions (id, created_at, last_seen)"
    " SELECT id, created_at, last_seen"
    " FROM migration_legacy_rating_sessions")
orph = one("SELECT COUNT(*) c FROM migration_legacy_rating_nonces r"
           " LEFT JOIN lexemes l ON l.id = r.entry_id WHERE l.id IS NULL")["c"]
run("INSERT IGNORE INTO rating_nonces (nonce, session_id, lexeme_id,"
    " created_at, expires_at) SELECT r.nonce, r.session_id, r.entry_id,"
    " r.created_at, r.expires_at FROM migration_legacy_rating_nonces r"
    " JOIN lexemes l ON l.id = r.entry_id")
log(f"   {cur.rowcount} Rating-Nonces ({orph} verwaist übersprungen)")
orph = one("SELECT COUNT(*) c FROM migration_legacy_image_moves r"
           " LEFT JOIN lexemes l ON l.id = r.from_entry_id WHERE l.id IS NULL")["c"]
run("INSERT INTO image_moves (id, filename, from_lexeme_id, target_word,"
    " language, status, note, created_at, decided_at)"
    " SELECT r.id, r.filename, r.from_entry_id, r.target_word, r.language,"
    " r.status, r.note, r.created_at, r.decided_at"
    " FROM migration_legacy_image_moves r"
    " JOIN lexemes l ON l.id = r.from_entry_id")
log(f"   {cur.rowcount} Umzugswünsche ({orph} verwaist übersprungen)")
run("INSERT IGNORE INTO speaker_ratings (language, voice, gut, session_id,"
    " created_at) SELECT language, voice, gut, session_id, created_at"
    " FROM migration_legacy_speaker_ratings")
conn.commit()

log("== 9/10 Lesarten + Audiodateien ==")
r_rows = [(e["id"], e["id"], "migration") for e in entries]
executemany("INSERT IGNORE INTO readings (form_id, lexeme_id, source)"
            " VALUES (%s,%s,%s)", r_rows)
# Audiodateien: <lang>-<id>-<voice>[-ls<x>].wav; stimmenlose Altdateien
# (<lang>-<id>.wav) → voice='' = Default-Stimme der Sprache.
# _t-*-Dateien sind Test-Cache, kein Bestand, werden nicht migriert.
pat_voice = re.compile(
    r"^([a-z]{2,3})-(\d+)-([A-Za-z0-9_]+?)(-ls([\d.]+))?\.wav$")
pat_plain = re.compile(r"^([a-z]{2,3})-(\d+)\.wav$")
audio_rows, skipped_audio = [], []
if os.path.isdir(AUDIO_DIR):
    for fn in sorted(os.listdir(AUDIO_DIR)):
        m = pat_voice.match(fn) or pat_plain.match(fn)
        if not m:
            skipped_audio.append(fn)
            continue
        lang, entry_id = m.group(1), int(m.group(2))
        if m.re is pat_voice:
            voice, ls = m.group(3), float(m.group(5) or 1.0)
        else:
            voice, ls = "", 1.0
        audio_rows.append((entry_id, entry_id, voice, ls, fn))
executemany("INSERT IGNORE INTO reading_audio (reading_id, voice,"
            " length_scale, filename)"
            " VALUES ((SELECT id FROM readings WHERE form_id=%s"
            "          AND lexeme_id=%s), %s, %s, %s)",
            audio_rows)
log(f"   {len(r_rows)} Lesarten, {len(audio_rows)} Audiodateien"
    " referenziert")
if skipped_audio:
    log(f"   {len(skipped_audio)} Audiodateien nicht dem Modell zuordenbar"
        f" (Test-Cache o. ä.): {skipped_audio[:5]} ...")
conn.commit()

log("== 10/10 Alttabellen umbenennen (Verifikation danach) ==")
RENAME = ["entries", "definitions", "senses", "sense_definitions",
          "sense_translations", "sense_links", "grammar", "entry_images",
          "word_ratings"]
for t in RENAME:
    run(f"RENAME TABLE {t} TO migration_legacy_{t}")
conn.commit()

log("== Verifikation ==")
wf = one("SELECT COUNT(*) c FROM word_forms")["c"]
lx = one("SELECT COUNT(*) c FROM lexemes")["c"]
mn = one("SELECT COUNT(*) c FROM meanings")["c"]
le = one("SELECT COUNT(*) c FROM migration_legacy_entries")["c"]
sn = one("SELECT COUNT(*) c FROM migration_legacy_senses")["c"]
log(f"   word_forms {wf} (Alt entries {le})  -> {'OK' if wf >= le else 'FEHLER!'}")
log(f"   lexemes {lx}  -> {'OK' if lx == le else 'FEHLER!'}")
log(f"   meanings {mn} (Alt senses {sn})  -> {'OK' if mn == sn else 'FEHLER!'}")
for neu, alt in (("lexeme_defs", "definitions"),
                 ("meaning_definitions", "sense_definitions"),
                 ("lexeme_images", "entry_images"),
                 ("meaning_links", "sense_links"),
                 ("ratings", "ratings"),
                 ("lexeme_ratings", "word_ratings"),
                 ("source_links", "source_links"),
                 ("lexeme_examples", None)):
    n = one(f"SELECT COUNT(*) c FROM {neu}")["c"]
    if alt:
        a = one(f"SELECT COUNT(*) c FROM migration_legacy_{alt}")["c"]
        flag = "OK" if n >= a else "PRÜFEN"
        log(f"   {neu} {n} vs {alt} {a}  -> {flag}")
    else:
        log(f"   {neu} {n}  -> OK")
log("Migration fertig. Legacy-Tabellen: migration_legacy_*")
