# -*- coding: utf-8 -*-
"""Autonomes Sprachaudit der Legacy-Definitionen.

Beim Umstieg auf die Definitions-Matrix wurden alle bestehenden
Definitionen pauschal als 'de' uebernommen (source='legacy'). Dieses
Skript laesst ein Modell die Sprache jedes Textes erkennen und ordnet
falsch einsortierte Definitionen ihrer wahren Sprache zu:

  Text ist deutsch          -> source='legacy-checked' (bestaetigt)
  Text gehoert woanders hin -> definitions.lang umziehen, source=
                               'legacy-moved'; beim deutschen Slot wird
                               entries.definition geleert, damit die
                               Define-Pipeline spaeter eine echte deutsche
                               Bedeutung nachliefert
  Nicht erkennbar           -> source='legacy-unsure' (markiert, nicht
                               raten)

Nebenbei werden Defekte bereinigt, die aeltere Define-Laeufe hinterlassen
haben: als Python-Dict gespeicherte Texte ("{'Substantiv': '...'}") werden
in lesbare Form ueberfuehrt.

Aufruf (Pipeline, pro Zyklus ein Batzen):
  DICT_API_URL=... DICT_API_KEY=... MODEL_BASE_URL=... \
      python fix_def_lang.py --limit 100 --model mistral-large-4:cloud
"""
import argparse
import ast
import json
import os
import re
import sys
import time
import urllib.request

from harvester import api_get, api_patch, api_post  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ.get(
    "MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")

PROMPT = (
    "Welche Sprache haben die folgenden Wörterbuch-Definitionen? "
    "Antworte NUR mit einem JSON-Objekt der Form "
    '{"1":"de","2":"it",...} — Werte als ISO-639-1-Code der erkennbaren '
    "Sprache (de, en, es, fr, it, ru, zh, ja, hi, ar, ...), "
    '"de" auch bei Zwischenlagen Richtung Deutsch, "?" wenn die Sprache '
    "nicht sicher erkennbar ist.\n\n"
)
GUELTIG = re.compile(r"^[a-z]{2,3}$")


def saubere_repr(text: str) -> str:
    """Python-Dict-Repr in lesbare Definition ueberfuehren.

    alte Laeufe haben teils str(dict) gespeichert:
    "{'Substantiv': 'Brot aus ...'}" -> "Substantiv: Brot aus ..."
    """
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        return text
    try:
        val = ast.literal_eval(t)
    except Exception:
        return text
    if not isinstance(val, dict):
        return text
    parts = [f"{k}: {v}" if str(k).lower() not in str(v).lower() else str(v)
             for k, v in val.items()]
    return ", ".join(parts)


def sprachen_erkennen(model: str, texte: list[str]) -> dict:
    """Modell-Batch: {lfd_nummer: iso_code_oder_?}"""
    nummeriert = "\n".join(
        f"{i + 1}. {t[:300]}" for i, t in enumerate(texte))
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT + nummeriert}],
        "max_tokens": 500,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{MODEL_BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MODEL_API_KEY}"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=180))
        out = r["choices"][0]["message"].get("content") or ""
    except Exception as e:
        print(f"  Modellfehler: {e}", file=sys.stderr)
        return {i + 1: "?" for i in range(len(texte))}
    m = re.search(r"\{.*\}", out, re.S)
    try:
        roh = json.loads(m.group(0)) if m else {}
    except Exception:
        roh = {}
    erg = {}
    for i in range(1, len(texte) + 1):
        code = str(roh.get(str(i), "?")).strip().lower()
        erg[i] = code if GUELTIG.match(code) else "?"
    return erg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100,
                    help="Definitionen pro Lauf (Batch à 10)")
    ap.add_argument("--model", default=os.environ.get(
        "MODEL_NAME", "mistral-large-4:cloud"))
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()

    rows = api_get("/definitions/audit-queue?" +
                   f"limit={a.limit}").get("rows", [])
    if not rows:
        print("audit-queue: nichts zu prüfen")
        return
    print(f"{len(rows)} Legacy-Definitionen zu prüfen via {a.model}",
          flush=True)

    stat = {"confirm": 0, "move": 0, "unsure": 0, "sauber": 0}
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        codes = sprachen_erkennen(a.model, [r["text"] for r in chunk])
        for n, r in enumerate(chunk, 1):
            eid, elang = r["entry_id"], r["language"]
            text = saubere_repr(r["text"])
            if text != r["text"]:
                api_patch(f"/entries/{eid}/definitions",
                          {"lang": "de", "text": text, "source": "legacy"})
                stat["sauber"] += 1
            code = codes.get(n, "?")
            if code == "de":
                api_post(f"/entries/{eid}/definitions/audit",
                         {"action": "confirm", "source": "legacy-checked"})
                stat["confirm"] += 1
            elif code == "?":
                api_post(f"/entries/{eid}/definitions/audit",
                         {"action": "confirm", "source": "legacy-unsure"})
                stat["unsure"] += 1
            else:
                api_post(f"/entries/{eid}/definitions/audit",
                         {"action": "move", "lang": code,
                          "source": "legacy-moved",
                          "clear_entry_def": elang == "de"})
                stat["move"] += 1
        print(f"  {min(i + a.batch, len(rows))}/{len(rows)}: {stat}",
              flush=True)
        time.sleep(a.delay)
    print(f"fertig: {stat}")


if __name__ == "__main__":
    main()
