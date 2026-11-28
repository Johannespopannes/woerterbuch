# -*- coding: utf-8 -*-
"""Ablösen des Legacy-Bestands: migration_legacy_*-Tabellen entfernen.

Nur ausführen, wenn die Migration verifiziert vollständig ist und das
System produktiv läuft (Backup-Dump vorhanden). Zählt jede Tabelle vor
dem DROP und bricht ab, wenn unerwartete Namen auftauchen.

Aufruf auf dem Pi:
    set -a; . .env; set +a
    .venv/bin/python tools/drop_legacy.py
"""
import os
import sys

import pymysql

DB_NAME = os.environ.get("WB_MYSQL_DB", "woerterbuch")

conn = pymysql.connect(user=os.environ.get("WB_MYSQL_USER", "woerterbuch"),
                       password=os.environ["WB_MYSQL_PW"],
                       database=DB_NAME,
                       charset="utf8mb4",
                       unix_socket="/run/mysqld/mysqld.sock",
                       cursorclass=pymysql.cursors.DictCursor,
                       autocommit=False)
cur = conn.cursor()


def q(sql, params=None):
    cur.execute(sql, params or ())
    return list(cur.fetchall())


legacy = [r["TABLE_NAME"] for r in q(
    "SELECT TABLE_NAME FROM information_schema.TABLES"
    " WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'migration_legacy_%%'",
    (DB_NAME,))]

if not legacy:
    print("Keine migration_legacy_*-Tabellen vorhanden — nichts zu tun.")
    sys.exit(0)

ERWARTET = {
    "migration_legacy_entries", "migration_legacy_definitions",
    "migration_legacy_senses", "migration_legacy_sense_definitions",
    "migration_legacy_sense_translations", "migration_legacy_sense_links",
    "migration_legacy_grammar", "migration_legacy_entry_images",
    "migration_legacy_word_ratings",
    "migration_legacy_ratings", "migration_legacy_source_links",
    "migration_legacy_search_stats", "migration_legacy_rating_sessions",
    "migration_legacy_rating_nonces", "migration_legacy_speaker_ratings",
    "migration_legacy_image_moves",
}
unbekannt = set(legacy) - ERWARTET
if unbekannt:
    print(f"!! Unerwartete Legacy-Tabellen: {sorted(unbekannt)} — Abbruch.")
    sys.exit(1)

# Fremdschlüssel zwischen Legacy-Tabellen (z. B. senses -> entries)
# während des Drops außer Kraft setzen.
cur.execute("SET FOREIGN_KEY_CHECKS=0")
for t in sorted(legacy):
    n = q(f"SELECT COUNT(*) c FROM `{t}`")[0]["c"]
    cur.execute(f"DROP TABLE `{t}`")
    print(f"   gedroppt: {t} ({n} Zeilen)", flush=True)

conn.commit()
cur.execute("SET FOREIGN_KEY_CHECKS=1")
conn.commit()
print(f"Fertig: {len(legacy)} Legacy-Tabellen entfernt.")
