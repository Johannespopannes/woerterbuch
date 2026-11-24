# -*- coding: utf-8 -*-
"""klingon-dict-raw.json -> klingon-import.sql (MariaDB, LIVE-DB-Schema).

Einmal-Migration (Präzedenz merge_ar_dupes): läuft nicht in der Pipeline.
INSERT IGNORE = idempotent gegen UNIQUE(word, language) bzw. grammar-Unique.
Logik (POS_DE/pos_de/word_class) gespiegelt aus gen_klingon_import.py.
"""
import json

POS_DE = [
    ("v:t_c", "transitives Verb"),
    ("v:t", "transitives Verb"),
    ("v:is", "intransitives Verb"),
    ("v:i", "intransitives Verb"),
    ("v", "Verb"),
    ("n:being", "Substantiv (Lebewesen)"),
    ("n:anim", "Substantiv (Tier)"),
    ("n:body", "Substantiv (Körperteil)"),
    ("n:place", "Substantiv (Ort)"),
    ("n:name", "Substantiv (Name)"),
    ("n", "Substantiv"),
    ("sen:phr", "Redewendung"),
    ("sen", "Satz"),
    ("adv", "Adverb"),
    ("conj", "Konjunktion"),
    ("excl", "Ausruf"),
    ("num", "Zahlwort"),
    ("pro", "Pronomen"),
    ("ques", "Fragepartikel"),
    ("pref", "Präfix"),
    ("suff", "Suffix"),
    ("affix", "Affix"),
]


def pos_de(pos):
    for prefix, de in POS_DE:
        if pos == prefix or pos.startswith(prefix + ",") \
                or pos.startswith(prefix + ":"):
            return de
    return "Andere (" + pos + ")"


def word_class(pos):
    if pos.startswith("v"):
        return "verb"
    if pos.startswith("n"):
        return "noun"
    if pos.startswith("sen"):
        return "phrase"
    if pos.startswith("adv"):
        return "adverb"
    if pos.startswith("conj"):
        return "conjunction"
    if pos.startswith("excl"):
        return "exclamation"
    if pos.startswith("num"):
        return "numeral"
    if pos.startswith("pro"):
        return "pronoun"
    if pos.startswith("ques"):
        return "question-word"
    if pos.startswith(("pref", "suff", "affix")):
        return "affix"
    return "other"


def esc(s):
    if s is None:
        return "NULL"
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


with open("klingon-dict-raw.json", encoding="utf-8") as f:
    db = json.load(f)

zeilen = [
    "-- Klingonisch-Import (boQwI' De7vID/klingon-assistant-data, DB-Version "
    + db.get("meta", {}).get("db_version", "?")
    + ") — generiert, nicht von Hand editieren",
    "-- idempotent: INSERT IGNORE gegen UNIQUE(word,language) / grammar-Unique",
]
n = 0
for key, e in db["qawHaq"].items():
    pos = e.get("part_of_speech", "")
    definition_de = (e.get("definition") or {}).get("de", "")
    definition_en = (e.get("definition") or {}).get("en", "")
    notes = e.get("notes") or {}
    examples = e.get("examples") or {}
    evidence = {
        "pos_raw": pos,
        "definition_en": definition_en,
        "notes": {k: v for k, v in notes.items() if v},
        "examples": {k: v for k, v in examples.items() if v},
        "components": e.get("components", ""),
        "see_also": e.get("see_also", ""),
        "synonyms": e.get("synonyms", ""),
    }
    wort = e.get("entry_name") or key
    definition = (pos_de(pos) + ": " + definition_de
                  if definition_de else pos_de(pos))
    zeilen.append(
        "INSERT IGNORE INTO entries"
        " (word, language, definition, source, lemma, confidence, evidence)"
        " VALUES ({}, 'tlh', {}, 'boQwI''', {}, 1.0, {});".format(
            esc(wort), esc(definition), esc(wort),
            esc(json.dumps(evidence, ensure_ascii=False))))
    zeilen.append(
        "INSERT IGNORE INTO grammar"
        " (language, lemma, word_class, gender, article, flexion, source)"
        " VALUES ('tlh', {}, {}, NULL, NULL, NULL, 'boQwI''');".format(
            esc(wort), esc(word_class(pos))))
    n += 1

with open("klingon-import.sql", "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(zeilen) + "\n")
print(f"SQL geschrieben: {n} Einträge + {n} Grammatikzeilen")
