# -*- coding: utf-8 -*-
"""Importiert klingon-import.jsonl in woerterbuch.db auf dem Pi."""
import json, sqlite3, shutil, sys

DB = "/mnt/disk2/woerterbuch/woerterbuch.db"
shutil.copy(DB, "/tmp/woerterbuch.db.bak.klingon")

with open("/tmp/klingon-import.jsonl", encoding="utf-8") as f:
    data = json.load(f)

con = sqlite3.connect(DB)
cur = con.cursor()
n_ins = n_skip = 0
for e in data["entries"]:
    try:
        cur.execute(
            "INSERT INTO entries (word, language, definition, source, lemma, confidence, evidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (e["word"], e["language"], e["definition"], e["source"], e["lemma"], e["confidence"], e["evidence"]),
        )
        n_ins += 1
    except sqlite3.IntegrityError:
        n_skip += 1
g_ins = 0
for g in data["grammar"]:
    cur.execute(
        "INSERT OR IGNORE INTO grammar (language, lemma, word_class, gender, article, flexion, source) "
        "VALUES (?,?,?,?,?,?,?)",
        (g["language"], g["lemma"], g["word_class"], g["gender"], g["article"], g["flexion"], g["source"]),
    )
    g_ins += cur.rowcount
con.commit()
print("entries inserted:", n_ins, "skipped (dupes):", n_skip)
print("grammar inserted:", g_ins)
print("tlh entries total:", cur.execute("SELECT COUNT(*) FROM entries WHERE language='tlh'").fetchone()[0])
con.close()
