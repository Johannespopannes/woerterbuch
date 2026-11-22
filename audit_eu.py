"""EU-Audit: Stichwörter objektiv gegen Wörterbuch-Quellen prüfen.

Für jeden Eintrag ohne Evidenz wird geprüft, ob ein etabliertes
Nachschlagewerk ihn führt:
  de → DWDS-Wörterbuch-API (https://www.dwds.de/api/wb/snippet)
  en/fr/es/it → Wiktionary (Existenz des Lemma-Artikels, 50 Titel/Request)

Ergebnis wird als Evidenz am Eintrag gespeichert:
  gefunden     → confidence 0.9, sprache_unsicher false
  nicht gefunden → confidence 0.3, sprache_unsicher true
Nichts wird gelöscht — das Modell-Audit (audit_words.py) bleibt die
zweite, unabhängige Stimme.

Budget pro Lauf begrenzt (default 400), damit der Pipeline-Schritt
zeitlich begrenzt bleibt; über die Zyklen wird der Bestand vollständig
abgedeckt (nur Einträge ohne Evidenz werden erneut geprüft).

Beispiel:
    python audit_eu.py --language it --budget 200
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = os.environ["DICT_API_URL"].rstrip("/")
KEY = os.environ["DICT_API_KEY"]
UA = {"User-Agent": "woerterbuch-audit/1.0 (privates Nachschlagewerk)"}

WIKTIONARY_LANGS = {"en", "fr", "es", "it", "ru", "zh", "ja", "hi"}


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


def fetch_unverified(language: str) -> list[dict]:
    """Einträge ohne Evidenz, älteste zuerst."""
    out, offset = [], 0
    while True:
        _, data = api("GET", f"/entries?language={language}"
                            f"&limit=1000&offset={offset}")
        batch = data or []
        out.extend(e for e in batch if not e.get("evidence"))
        if len(batch) < 1000:
            return out
        offset += 1000


def http_get_json(url: str, tries: int = 4) -> dict:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Abruf fehlgeschlagen: {url[:80]}")


def dwds_lookup(words: list[str]) -> dict[str, bool]:
    """DWDS-Snippet-API: gefundene Lemmata je Eingabewort."""
    q = urllib.parse.quote("|".join(words))
    data = http_get_json(f"https://www.dwds.de/api/wb/snippet/?q={q}")
    # Antwort: Liste von Trefferlisten pro Lemma-Gruppe
    found: dict[str, bool] = {}
    groups = data if isinstance(data, list) else [data]
    for i, group in enumerate(groups):
        if i < len(words):
            found[words[i]] = bool(group)
    # Sicherheitshalber fehlende Keys auffüllen
    return {w: found.get(w, False) for w in words}


def wiktionary_lookup(language: str, words: list[str]) -> dict[str, bool]:
    """Existenzprüfung gegen {lang}.wiktionary.org, 50 Titel pro Request."""
    titles = "|".join(urllib.parse.quote(w) for w in words)
    url = (f"https://{language}.wiktionary.org/w/api.php"
           f"?action=query&format=json&titles={titles}")
    data = http_get_json(url)
    pages = data.get("query", {}).get("pages", {})
    present = {p.get("title", "").lower()
               for p in pages.values() if "missing" not in p}
    # Titel kommen kanonisch zurück; Prüfung case-insensitiv auf Wort
    return {w: (w.lower() in present or w.capitalize().lower() in present)
            for w in words}


def lookup(language: str, batch: list[str]) -> dict[str, bool]:
    if language == "de":
        return dwds_lookup(batch)
    if language in WIKTIONARY_LANGS:
        return wiktionary_lookup(language, batch)
    raise SystemExit(f"Sprache nicht unterstützt: {language}")


def save(entry_id: int, found: bool, quelle: str, detail: str = "") -> None:
    body = {
        "confidence": 0.9 if found else 0.3,
        "sprache_unsicher": not found,
        "evidence": ({"quelle": quelle}
                     if found else
                     {"quelle": quelle, "hinweis": detail or
                      f"nicht in {quelle} gefunden"}),
    }
    code, _ = api("POST", f"/entries/{entry_id}/evidence", body)
    if code != 200:
        print(f"  FEHLER {code} bei Eintrag #{entry_id}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--language", required=True)
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--batch", type=int, default=40)
    args = ap.parse_args()

    quelle = "DWDS" if args.language == "de" else "Wiktionary"
    pending = fetch_unverified(args.language)[: args.budget]
    print(f"[{args.language}] objektive Prüfung: {len(pending)} Einträge "
          f"(ohne Evidenz), Quelle: {quelle}")
    found_n = missing_n = 0
    for i in range(0, len(pending), args.batch):
        batch = pending[i: i + args.batch]
        try:
            verdicts = lookup(args.language, [e["word"] for e in batch])
        except Exception as ex:
            print(f"  Batch-Fehler: {ex}", file=sys.stderr)
            continue
        for e in batch:
            ok = verdicts.get(e["word"], False)
            if ok:
                found_n += 1
            else:
                missing_n += 1
                print(f"  FEHLT: {e['word']}")
            save(e["id"], ok, quelle)
        time.sleep(0.7)
    print(f"[{args.language}] geprüft: {found_n + missing_n}, "
          f"bekannt: {found_n}, unsicher markiert: {missing_n}")


if __name__ == "__main__":
    main()
