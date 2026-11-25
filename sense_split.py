# -*- coding: utf-8 -*-
"""Autonome Sinntrennung: Woerter mit Definition in Bedeutungen zerlegen.

Ein Wort, mehrere Bedeutungen: 'Bank' = Finanzinstitut vs. Sitzmoebel.
Das Modell bekommt Wort + vorhandene Wort-/Legacy-Definition als HINWEIS
und schlaegt Sinne mit kurzer Glosse und eigenstaendiger Beschreibung vor.
WICHTIG: Altdefinitionen werden NICHT umverteilt — Sinne bekommen neue,
eigens erzeugte Beschreibungen (source='sense-split'). Unsicherheiten
bleiben auf Wortebene, wo sie herkommen.

Aufruf (Pipeline, je Sprachkette):
  DICT_API_URL=... DICT_API_KEY=... MODEL_BASE_URL=... \
      python sense_split.py --language de --limit 150 --batch 5
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

from harvester import api_get, api_post, api_put, post_chat_completions  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ.get(
    "MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")

PROMPT = (
    "Wörterbuch-Einträge. Zerlege jedes Wort in seine BEDEUTUNGEN "
    "(Sinne). Homonyme (gleiche Schreibung, verschiedene Ursprünge) "
    "und verschiedene Wortarten bekommen eigene Sinne. Pro Sinn: "
    "label (kurze Glosse), wortart (Substantiv/Verb/Adjektiv/... oder "
    "leer), beschreibung (ein Satz, Sprache des Eintrags). Antworte "
    'NUR mit einem JSON-Objekt {"1":[{"label":"...","wortart":"...",'
    '"beschreibung":"..."}],"2":[...],...}.\n\n'
)


def sinne_bilden(model: str, zeilen: list[str]) -> dict:
    """Modell-Batch: {lfd_nr: [{label, beschreibung}]}"""
    nummeriert = "\n".join(f"{i + 1}. {z}" for i, z in enumerate(zeilen))
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT + nummeriert}],
        "max_tokens": 1200,
        "temperature": 0,
    }
    # Request-Aufbau, Retry und 429-Fallback (Cloud-Quota) zentral in
    # harvester.post_chat_completions.
    try:
        r = post_chat_completions(body, int(os.environ.get('MODEL_TIMEOUT', '240')))
        out = r["choices"][0]["message"].get("content") or ""
    except Exception as e:
        print(f"  Modellfehler: {e}", file=sys.stderr)
        return {}
    m = re.search(r"\{.*\}", out, re.S)
    try:
        roh = json.loads(m.group(0)) if m else {}
    except Exception:
        roh = {}
    erg = {}
    for k, v in roh.items():
        if isinstance(v, list):
            sinne = []
            for s in v[:6]:
                if isinstance(s, dict) and s.get("label"):
                    sinne.append({
                        "label": str(s["label"])[:200],
                        "wortart": str(s.get("wortart", ""))[:40],
                        "beschreibung": str(s.get("beschreibung", ""))[:1000],
                    })
            if sinne:
                erg[str(k)] = sinne
    return erg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--language", required=True)
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--model", default=os.environ.get(
        "MODEL_NAME", "mistral-large-4:cloud"))
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()
    lang = a.language.lower()

    rows = api_get(f"/senses/queue?language={lang}&limit={a.limit}"
                   ).get("rows", [])
    if not rows:
        print(f"{lang}: Sinntrennung — nichts zu tun")
        return
    print(f"{lang}: {len(rows)} Einträge zur Sinntrennung via {a.model}",
          flush=True)

    stat = {"sinne": 0, "woerter": 0}
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        zeilen = [f"{r['word']} — {str(r.get('definition') or '')[:200]}"
                  for r in chunk]
        vorschlaege = sinne_bilden(a.model, zeilen)
        for n, r in enumerate(chunk, 1):
            sinne = vorschlaege.get(str(n))
            if not sinne:
                continue
            for s in sinne:
                resp = api_post(f"/entries/{r['id']}/senses",
                                {"label": s["label"],
                                 "word_class": s.get("wortart", ""),
                                 "source": "sense-split"})
                sid = resp["sense_id"]
                if s["beschreibung"]:
                    api_put(f"/senses/{sid}/definitions",
                            {"lang": lang, "text": s["beschreibung"],
                             "source": "sense-split"})
                stat["sinne"] += 1
            stat["woerter"] += 1
        print(f"  {min(i + a.batch, len(rows))}/{len(rows)}: {stat}",
              flush=True)
        time.sleep(a.delay)
    print(f"{lang} fertig: {stat}")


if __name__ == "__main__":
    main()
