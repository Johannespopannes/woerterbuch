"""Hamza-Dubletten zusammenführen (ar/tn) — mit Jais-Entscheid.

Hintergrund: die zwischenzeitlich korrigierte Hamza-Faltung hat Dubletten
erzeugt (اسرة neben أسرة). Dieses Skript findet Kandidaten über den
Fold-Schlüssel (gleiche Buchstaben nach Hamza-Faltung), lässt Jais je
Gruppe entscheiden, ob es sich um DUBletten derselben Wortform handelt
oder um verschiedene Wörter (سأل vs. سال!), und führt nur bestätigte
Dubletten zusammen: Votes, Galerie-Bilder, Lemma/Form/Definition wandern
auf den kanonischen Eintrag, die Dublette wird gelöscht.

Trockenlauf ist Standard: erst mit --apply wirklich mergen.

Aufruf: python merge_ar_dupes.py [--apply] [--language ar --language tn]
Env: DICT_API_URL, DICT_API_KEY, MODEL_BASE_URL, MODEL_NAME
"""

import argparse
import json
import os
import re
import sqlite3  # nur für SQL-Escaping-Fehler vorab, Zugriff läuft über ssh
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.environ["DICT_API_URL"].rstrip("/")
KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ["MODEL_BASE_URL"].rstrip("/")
MODEL_NAME = os.environ["MODEL_NAME"]
DB = "/home/johannes/woerterbuch/data/woerterbuch.db"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harvester import AR_FOLD, normalize_ar  # noqa: E402

SSH = ["ssh", "-J", "root@v65200.php-friends.de",
       "-i", os.path.expanduser("~/.ssh/id_ed25519"),
       "-o", "StrictHostKeyChecking=accept-new", "johannes@10.77.0.2"]

DUP_PROMPT = (
    "لديّ هذه الكلمات من قاموس عربي: {words}.\n"
    "بعضها قد تكون نسخًا مكررة لنفس الكلمة تختلف فقط في الهمزات "
    "(همزة مفقودة أو زائدة = خطأ إملائي)، وبعضها كلمات مختلفة المعنى "
    "تتشابه شكلها بدون همزات (مثل سأل وسال — هاتان مختلفتان!).\n"
    "أجب بكائن JSON فقط بهذا الشكل:\n"
    "{{\"dup\": [[\"الكلمة بالرسم الصحيح\", \"النسخة الخاطئة\"], ...], "
    "\"distinct\": [\"كلمات يجب أن تبقى منفصلة\"]}}\n"
    "إذا لم توجد تكرارات استخدم {{\"dup\": [], \"distinct\": [...]}}."
)


def fold_key(word: str) -> str:
    w = normalize_ar(word)
    return "".join(AR_FOLD.get(c, c) for c in w)


def api(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "X-API-Key": KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def chat(prompt: str) -> str:
    req = urllib.request.Request(
        f"{MODEL_BASE_URL}/chat/completions",
        data=json.dumps({
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 4000,
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer dummy"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("leere Antwort")
    return content


def parse_object(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    return json.loads(m.group(0) if m else raw)


def sqlite(sql: str) -> None:
    proc = subprocess.run(SSH + [f"sqlite3 {DB} \"{sql}\""],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"sqlite-Fehler: {proc.stderr.strip()[:300]}")


def merge_sql(canon: int, dup: int) -> str:
    return f"""
BEGIN;
UPDATE ratings SET entry_id={canon} WHERE entry_id={dup};
UPDATE word_ratings SET entry_id={canon} WHERE entry_id={dup};
INSERT OR IGNORE INTO entry_images (entry_id, filename, created_at)
    SELECT {canon}, filename, created_at FROM entry_images WHERE entry_id={dup};
DELETE FROM entry_images WHERE entry_id={dup};
UPDATE entries SET image=(SELECT image FROM entries WHERE id={dup})
    WHERE id={canon} AND image IS NULL;
UPDATE entries SET definition=(SELECT definition FROM entries WHERE id={dup})
    WHERE id={canon} AND (definition IS NULL OR definition='');
UPDATE entries SET lemma=(SELECT lemma FROM entries WHERE id={dup})
    WHERE id={canon} AND lemma IS NULL;
UPDATE entries SET form=(SELECT form FROM entries WHERE id={dup})
    WHERE id={canon} AND form IS NULL;
DELETE FROM entries WHERE id={dup};
COMMIT;
""".strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--language", action="append", default=None,
                    help="Standard: ar und tn")
    args = ap.parse_args()
    if not args.language:
        args.language = ["ar", "tn"]

    for language in args.language:
        entries: list[dict] = []
        offset = 0
        while True:
            _, batch = api("GET", f"/entries?language={language}"
                                  f"&limit=1000&offset={offset}")
            batch = batch or []
            entries.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(fold_key(e["word"]), []).append(e)
        multi = {k: v for k, v in groups.items() if len(v) > 1}
        print(f"[{language}] {len(entries)} Einträge, "
              f"{len(multi)} Fold-Gruppen mit mehreren Einträgen")

        merged = kept = 0
        for key, members in multi.items():
            words = [m["word"] for m in members]
            try:
                verdict = parse_object(chat(
                    DUP_PROMPT.format(words=json.dumps(
                        words, ensure_ascii=False))))
            except Exception as ex:
                print(f"  Gruppe {words}: Batch-Fehler: {ex}",
                      file=sys.stderr)
                continue
            by_word = {m["word"]: m for m in members}
            for pair in verdict.get("dup", []):
                if not isinstance(pair, list) or len(pair) < 2:
                    continue
                correct = str(pair[0]).strip()
                dup_words = [str(w).strip() for w in pair[1:]]
                canon = by_word.get(correct)
                if canon is None:
                    continue  # Jais-Schreibweise existiert nicht → Restore
                for dw in dup_words:
                    dup = by_word.get(dw)
                    if dup is None or dup["id"] == canon["id"]:
                        continue
                    print(f"  MERGE {dup['word']} -> {canon['word']} "
                          f"(#{dup['id']} → #{canon['id']})")
                    if args.apply:
                        try:
                            sqlite(merge_sql(canon["id"], dup["id"]))
                            merged += 1
                        except Exception as ex:
                            print(f"    FEHLER: {ex}", file=sys.stderr)
                    else:
                        merged += 1
            kept += len(verdict.get("distinct", []))
            time.sleep(0.3)
        mode = "gemergt" if args.apply else "mergebar (Trockenlauf)"
        print(f"[{language}] {mode}: {merged}, als verschieden erkannt: "
              f"{kept}")


if __name__ == "__main__":
    main()
