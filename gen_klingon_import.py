# -*- coding: utf-8 -*-
"""klingon-dict-raw.json -> JSONL fuer Import in woerterbuch.db auf dem Pi."""
import json, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

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
        if pos == prefix or pos.startswith(prefix + ",") or pos.startswith(prefix + ":"):
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

with open(r"C:\Repos\modelhub.tassekaffee.com\klingon-dict-raw.json", encoding="utf-8") as f:
    db = json.load(f)

entries = []
grammar = []
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
    entries.append({
        "word": e.get("entry_name") or key,
        "language": "tlh",
        "definition": pos_de(pos) + ": " + definition_de if definition_de else pos_de(pos),
        "source": "boQwI'",
        "lemma": e.get("entry_name") or key,
        "confidence": 1.0,
        "evidence": json.dumps(evidence, ensure_ascii=False),
    })
    grammar.append({
        "language": "tlh",
        "lemma": e.get("entry_name") or key,
        "word_class": word_class(pos),
        "gender": None,
        "article": None,
        "flexion": None,
        "source": "boQwI'",
    })

out = {"entries": entries, "grammar": grammar}
with open(r"C:\Repos\modelhub.tassekaffee.com\klingon-import.jsonl", "w", encoding="utf-8") as f:
    f.write(json.dumps(out, ensure_ascii=False))
print("entries:", len(entries), "grammar:", len(grammar))
