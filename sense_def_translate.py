# -*- coding: utf-8 -*-
"""Sinn-Beschreibungs-Matrix: jede Bedeutung in jeder Sprache erklärt.

Übersetzt die Beschreibungen von Sinnen (an den SINN gebunden, nicht
nur am Wort) von der Ausgangssprache ins Ziel. Läuft rotierend über
die Zielsprachen, ein Ziel pro Pipeline-Zyklus — wie define_langs,
nur eine Ebene tiefer.

Aufruf:
  DICT_API_URL=... DICT_API_KEY=... MODEL_BASE_URL=... \
      python sense_def_translate.py --from de --to it --limit 120
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from harvester import api_get, api_put  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ.get(
    "MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")

PROMPT = (
    "Übersetze die folgenden kurzen Wörterbuch-BESCHREIBUNGEN (jede "
    "beschreibt eine bestimmte Bedeutung eines Worts) ins {ziel}. "
    "Eine Zeile pro Aufgabe, nur die Übersetzung, keine Erklärung. "
    "Antworte NUR mit einem JSON-Objekt "
    '{{"1":"übersetzung","2":"..."}} — "?" wenn nicht übersetzbar.\n\n'
)


def uebersetzungen_holen(model: str, ziel: str, zeilen: list[str]) -> dict:
    nummeriert = "\n".join(f"{i + 1}. {z}" for i, z in enumerate(zeilen))
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(ziel=ziel) + nummeriert}],
        "max_tokens": 800,
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
        return {}
    m = re.search(r"\{.*\}", out, re.S)
    try:
        roh = json.loads(m.group(0)) if m else {}
    except Exception:
        roh = {}
    return {str(k): str(v).strip() for k, v in roh.items()
            if str(v).strip() and str(v).strip() != "?"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--model", default=os.environ.get(
        "MODEL_NAME", "mistral-large-4:cloud"))
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()
    frm, to = a.frm.lower(), a.to.lower()

    rows = api_get("/senses/def-queue?" + urllib.parse.urlencode(
        {"from": frm, "to": to, "limit": a.limit})).get("rows", [])
    if not rows:
        print(f"{frm}->{to}: keine Sinnbeschreibungen ohne {to}-Version")
        return
    print(f"{frm}->{to}: {len(rows)} Sinnbeschreibungen via {a.model}",
          flush=True)

    stat = {"uebersetzt": 0}
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        zeilen = [f"{r['word']} — {r['label']}: "
                  f"{str(r.get('text') or '')[:250]}" for r in chunk]
        uebers = uebersetzungen_holen(a.model, to, zeilen)
        for n, r in enumerate(chunk, 1):
            txt = uebers.get(str(n))
            if not txt:
                continue
            try:
                api_put(f"/senses/{r['sense_id']}/definitions",
                        {"lang": to, "text": txt[:1000],
                         "source": f"translate:{frm}"})
                stat["uebersetzt"] += 1
            except Exception as e:
                print(f"  PUT Sinn {r['sense_id']}: {e}", file=sys.stderr)
        print(f"  {min(i + a.batch, len(rows))}/{len(rows)}: {stat}",
              flush=True)
        time.sleep(a.delay)
    print(f"{frm}->{to} fertig: {stat}")


if __name__ == "__main__":
    main()
