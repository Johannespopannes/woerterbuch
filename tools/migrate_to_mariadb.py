"""Einmal-Migration: SQLite-Bestand nach MariaDB übertragen.

Läuft auf dem Pi (liest data/woerterbuch.db, schreibt via mariadb-CLI
als root über den Unix-Socket — kein PyMySQL nötig). IDs werden
übernommen, damit Referenzen (ratings.entry_id etc.) erhalten bleiben.

Aufruf: sudo python3 tools/migrate_to_mariadb.py [--db-name woerterbuch]
        [--sqlite /pfad/zur/woerterbuch.db]
"""

import argparse
import sqlite3
import subprocess
import sys

TABLES = [
    "entries", "grammar", "ratings", "word_ratings", "entry_images",
    "search_stats", "rating_sessions", "rating_nonces", "image_moves",
    "speaker_ratings",
]

# Referenzielle Integrität während des Aufbaus aus, am Ende prüfen
PREFIX = "SET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\n"
SUFFIX = "SET UNIQUE_CHECKS=1;\nSET FOREIGN_KEY_CHECKS=1;\n"


def esc(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def chunk_sql(table: str, cols: list[str], rows: list[tuple],
              n: int = 400) -> str:
    collist = ", ".join(cols)
    stmts = []
    for i in range(0, len(rows), n):
        part = rows[i: i + n]
        values = ",\n".join(
            "(" + ", ".join(esc(v) for v in row) + ")" for row in part)
        stmts.append(f"INSERT INTO {table} ({collist}) VALUES\n{values};")
    return "\n".join(stmts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-name", default="woerterbuch")
    ap.add_argument("--sqlite", default="/home/johannes/woerterbuch/"
                                          "data/woerterbuch.db")
    ap.add_argument("--chunk", type=int, default=400)
    args = ap.parse_args()

    src = sqlite3.connect(args.sqlite)
    sql_parts = [PREFIX]
    for table in TABLES:
        cols = [r[1] for r in src.execute(
            f"PRAGMA table_info({table})")]
        if not cols:
            print(f"Übersprungen (fehlt in SQLite): {table}")
            continue
        rows = src.execute(f"SELECT {', '.join(cols)} "
                           f"FROM {table}").fetchall()
        sql_parts.append(f"-- {table}: {len(rows)} Zeilen")
        sql_parts.append(f"DELETE FROM {table};")
        if rows:
            sql_parts.append(
                chunk_sql(table, cols, rows, args.chunk))
        print(f"{table}: {len(rows)} Zeilen")
    sql_parts.append(SUFFIX)
    sql_parts.append(
        "SELECT (SELECT COUNT(*) FROM entries) AS eintraege,"
        "       (SELECT COUNT(*) FROM ratings) AS bildvotes,"
        "       (SELECT COUNT(*) FROM word_ratings) AS wortvotes;")

    proc = subprocess.run(
        ["mariadb", "--default-character-set=utf8mb4", args.db_name],
        input="\n".join(sql_parts), text=True, capture_output=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(1)
    print("Migration abgeschlossen.")


if __name__ == "__main__":
    main()
