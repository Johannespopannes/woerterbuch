"""Einmal-Wiederherstellung: Hamza-Schreibweise arabischer Einträge.

Zwischen der Einführung von normalize_ar (22.11. ~09:10) und der Korrektur
wurden Hamza-Varianten irrtümlich in der gespeicherten Lemmaform gefaltet
(سأل → سال). Dieses Skript lässt Jais die kanonische Rechtschreibung jedes
ar/tn-Eintrags nennen und setzt abweichende Formen per PATCH /entries/{id}/word
um (ID, Bild, Votes bleiben erhalten). Konflikte (Zielwort existiert) werden
nur gemeldet.

Aufruf: python restore_ar_hamza.py [--language ar] [--language tn]
Env: DICT_API_URL, DICT_API_KEY, MODEL_BASE_URL, MODEL_NAME
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = os.environ["DICT_API_URL"].rstrip("/")
KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ["MODEL_BASE_URL"].rstrip("/")
MODEL_NAME = os.environ["MODEL_NAME"]

ORTHO_PROMPT = (
    "المهمة: تصحيح الإملاء العربي. القائمة التالية كلمات قد تكون مفقودة "
    "همزاتها أو ألفها المقصورة أو تحوّلت همزاتها إلى ألف أو ياء أو واو "
    "خاطئة. اكتب كل كلمة بالرسم الإملائي القياسي الصحيح (مع الهمزات "
    "والألف المقصورة حيث يلزم)، ولا تغيّر شيئًا في الكلمات الصحيحة "
    "أصلًا. أجب فقط بمصفوفة JSON من النصوص بنفس تعداد وترتيب المدخلات، "
    "بدون شرح وبدون Markdown.\nالكلمات: {words}"
)


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


def parse_array(raw: str) -> list[str]:
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
    items = json.loads(m.group(0) if m else raw)
    return [str(x) for x in items]


def fetch_all(language: str) -> list[dict]:
    out, offset = [], 0
    while True:
        _, data = api("GET", f"/entries?language={language}"
                            f"&limit=1000&offset={offset}")
        batch = data or []
        out.extend(batch)
        if len(batch) < 1000:
            return out
        offset += 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--language", action="append", required=True)
    ap.add_argument("--batch", type=int, default=15)
    args = ap.parse_args()

    for language in args.language:
        entries = fetch_all(language)
        print(f"[{language}] Orthographie-Wiederherstellung: "
              f"{len(entries)} Einträge")
        fixed, conflicts, errors = 0, 0, 0
        for i in range(0, len(entries), args.batch):
            batch = entries[i: i + args.batch]
            prompt = ORTHO_PROMPT.format(words=json.dumps(
                [e["word"] for e in batch], ensure_ascii=False))
            try:
                corrected = parse_array(chat(prompt))
            except Exception as ex:
                print(f"  Batch-Fehler: {ex}", file=sys.stderr)
                errors += 1
                continue
            if len(corrected) != len(batch):
                print(f"  Batch unvollständig ({len(corrected)}/"
                      f"{len(batch)}) — übersprungen", file=sys.stderr)
                errors += 1
                continue
            for e, corr in zip(batch, corrected):
                if corr == e["word"]:
                    continue
                code, _ = api("PATCH", f"/entries/{e['id']}/word",
                              {"word": corr})
                if code == 200:
                    fixed += 1
                    print(f"  {e['word']} -> {corr}")
                elif code == 409:
                    conflicts += 1
                    print(f"  KONFLIKT #{e['id']}: {e['word']} -> {corr} "
                          f"(existiert bereits)")
                else:
                    print(f"  FEHLER {code}: {e['word']}", file=sys.stderr)
            time.sleep(0.3)
        print(f"[{language}] korrigiert: {fixed}, Konflikte: {conflicts}, "
              f"fehlerhafte Batches: {errors}")


if __name__ == "__main__":
    main()
