# -*- coding: utf-8 -*-
"""Franco-Formen für tunesische Einträge erzeugen (Jais, lokal :8083).

Für jedes tn-Eintragswort die übliche Franco-Schreibweise (Lateinbuchstaben
+ Ziffern, z. B. صباح -> sbe7) erfragen, als eigenen Eintrag mit
script='latn' anlegen und über lemma mit dem arabischen Eintrag verknüpfen.
Lauf:  MODEL_BASE_URL=http://127.0.0.1:8083/v1 MODEL_NAME=jais-3-ar \
      DICT_API_URL=... DICT_API_KEY=... python tn_franco.py
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from harvester import api_get, chat  # noqa: E402  (Pfade via cwd woerterbuch)

import os
DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]

PROMPT = (
    "Du schreibst Tunesisch-Darja. Das Wort «{word}» steht in arabischer "
    "Schrift. Wie schreibt man es üblicherweise in Franco (lateinische "
    "Buchstaben plus Ziffern wie 3=ع, 7=ح, 5=خ, 9=ق, 2=ء)?\n"
    "Antworte mit GENAU einer Zeile im Format:\n"
    "FRANCO: <Schreibweise>"
)
FRANCO_RE = re.compile(r"^FRANCO:\s*([A-Za-z0-9 '’\-]{1,40})\s*$")
LATIN_RE = re.compile(r"^[A-Za-z0-9 '’\-]{1,40}$")


def post(path: str, body: dict):
    for versuch in range(4):
        req = urllib.request.Request(
            DICT_API_URL + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-API-Key": DICT_API_KEY}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            return None, e.code
        except Exception as e:
            # Verbindungsfehler (Deploy-Neustart, Netz) — kurz warten
            print(f"  POST {path}: {e} — Versuch {versuch + 1}/4",
                  file=sys.stderr)
            time.sleep(15 * (versuch + 1))
    return None, "unreachable"


def main() -> None:
    entries = api_get("/entries?" + urllib.parse.urlencode(
        {"language": "tn", "limit": 1000}))
    todo = [e for e in entries if not e.get("script")]
    print(f"{len(entries)} tn-Einträge, {len(todo)} ohne script-Angabe",
          flush=True)
    created = linked = failed = 0
    for e in todo:
        w = e["word"]
        try:
            out = chat(PROMPT.format(word=w), 0.1)
        except Exception as ex:
            print(f"  {w!r}: Jais-Fehler: {ex}", file=sys.stderr)
            failed += 1
            continue
        m = None
        for line in out.splitlines():
            m = FRANCO_RE.match(line.strip())
            if m:
                break
        franco = m.group(1).strip(" '’-") if m else ""
        if not franco or not LATIN_RE.match(franco):
            print(f"  {w!r}: keine brauchbare Franco-Form ({out[:60]!r})",
                  file=sys.stderr)
            failed += 1
            continue
        if franco.lower() == w.lower():
            print(f"  {w!r}: Franco = Wort selbst, übersprungen",
                  file=sys.stderr)
            continue
        body, err = post("/entries", {
            "word": franco, "language": "tn", "script": "latn",
            "source": "jais-franco"})
        if err == 409:
            print(f"  {franco!r} existiert schon")
        elif err:
            print(f"  {franco!r}: POST-Fehler {err}", file=sys.stderr)
            failed += 1
            continue
        else:
            created += 1
        # Lemma-Verknüpfung: beide Seiten auf dasselbe Lemma (arabisch)
        post("/formen/batch", {"items": [
            {"word": franco, "language": "tn", "lemma": w,
             "form": "Franco-Schreibweise"},
            {"word": w, "language": "tn", "lemma": w, "form": "Grundform"},
        ]})
        linked += 1
        print(f"  {w} -> {franco}", flush=True)
        time.sleep(0.3)
    print(f"fertig: {created} angelegt, {linked} verknüpft, {failed} Fehler")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
