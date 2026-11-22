"""Audit für Arabisch/Tunesisch: Altbestand aufräumen (Pipeline-Schritt).

1. Normalisierung: Einträge, die Vokalzeichen/Tatweel enthalten, auf die
   kanonische unvokalisierte Form umbenennen (PATCH /entries/{id}/word —
   behält ID, Bild, Votes). Konflikte (Zielwort existiert) werden nur
   gemeldet, nicht zusammengeführt.
2. Verifikation: Einträge aus der Kolibri-Zeit (source model:mistral-small-4)
   werden von Jais geprüft — Kolibri kann kein Arabisch und hat ggf.
   Fantasiewörter eingeschleust. Zweifelhafte werden per Evidence-Endpunkt
   als sprache_unsicher markiert (nichts wird gelöscht).

Aufruf: python audit_ar.py [--language ar|tn]
Env: DICT_API_URL, DICT_API_KEY, MODEL_BASE_URL, MODEL_NAME (jais-3-ar)
"""

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harvester import normalize_ar  # noqa: E402


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


def chat(prompt: str, max_tokens: int = 6000) -> str:
    req = urllib.request.Request(
        f"{MODEL_BASE_URL}/chat/completions",
        data=json.dumps({
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
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


def parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    return json.loads(m.group(0) if m else raw)


def fetch_entries(language: str) -> list[dict]:
    out, offset = [], 0
    while True:
        _, data = api("GET", f"/entries?language={language}"
                            f"&limit=1000&offset={offset}")
        batch = data or []
        out.extend(batch)
        if len(batch) < 1000:
            return out
        offset += 1000


def step_normalize(entries: list[dict], language: str) -> None:
    print(f"[{language}] Normalisierung: {len(entries)} Einträge")
    renamed, conflicts = 0, []
    for e in entries:
        word = e["word"]
        norm = normalize_ar(word)
        if norm == word:
            continue
        code, _ = api("PATCH", f"/entries/{e['id']}/word", {"word": norm})
        if code == 200:
            renamed += 1
            print(f"  {word} -> {norm}")
        elif code == 409:
            conflicts.append((word, norm, e["id"]))
        else:
            print(f"  FEHLER {code}: {word}", file=sys.stderr)
        time.sleep(0.1)
    print(f"[{language}] umbenannt: {renamed}, "
          f"Konflikte (manuell mergen): {len(conflicts)}")
    for word, norm, eid in conflicts:
        print(f"  KONFLIKT #{eid}: {word} vs. existierendes {norm}")


VERIFY_PROMPT = (
    "لكل كلمة في القائمة التالية حدد: هل هي كلمة حقيقية ومتداولة في "
    "اللغة {label}؟ أجب بكائن JSON فقط (بدون شرح، بدون Markdown): "
    "كل كلمة مفتاحاً وقيمتها كائن يحوي مفتاح \"ok\" (true/false) ومفتاح "
    "\"note\" (سبب موجز إذا false).\nالكلمات: {words}"
)

LABELS = {"ar": "العربية الفصحى", "tn": "الدارجة التونسية"}


def step_verify(entries: list[dict], language: str) -> None:
    stale = [e for e in entries
             if e.get("source") == "model:mistral-small-4"]
    if not stale:
        print(f"[{language}] keine Kolibri-Einträge zu prüfen")
        return
    print(f"[{language}] Verifikation: {len(stale)} Kolibri-Einträge")
    marked = 0
    for i in range(0, len(stale), 10):
        batch = stale[i:i + 10]
        words = [e["word"] for e in batch]
        prompt = VERIFY_PROMPT.format(
            label=LABELS.get(language, "العربية"), words=json.dumps(
                words, ensure_ascii=False))
        try:
            verdicts = parse_json_object(chat(prompt))
        except Exception as ex:
            print(f"  Batch-Fehler: {ex}", file=sys.stderr)
            continue
        for e in batch:
            v = verdicts.get(e["word"])
            if not isinstance(v, dict):
                continue
            if v.get("ok"):
                continue
            note = str(v.get("note", "laut Modell kein echtes Wort"))[:300]
            code, _ = api("POST", f"/entries/{e['id']}/evidence", {
                "confidence": 0.3,
                "sprache_unsicher": True,
                "evidence": {"hinweis": f"Jais-Audit: {note}",
                             "quelle": "jais-3-audit"},
            })
            if code == 200:
                marked += 1
                print(f"  UNSICHER: {e['word']} — {note}")
        time.sleep(0.3)
    print(f"[{language}] als unsicher markiert: {marked}")


def main() -> None:
    languages = ([sys.argv[sys.argv.index("--language") + 1]]
                 if "--language" in sys.argv else ["ar", "tn"])
    for language in languages:
        entries = fetch_entries(language)
        print(f"=== Audit {language}: {len(entries)} Einträge ===")
        step_normalize(entries, language)
        # nach Umbenennung frisch lesen, damit Verifikation neue Form sieht
        step_verify(fetch_entries(language), language)
        print(f"=== Audit {language} fertig ===")


if __name__ == "__main__":
    main()
