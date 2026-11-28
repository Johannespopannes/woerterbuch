# -*- coding: utf-8 -*-
"""Bedeutungsgerechte Uebersetzungsverbindungen zwischen Sinnen.

Fuer jeden Sinn (mit Definition in der Ausgangssprache) das Zielwort in
der Zielsprache erfragen und als Verbindung Sinn <-> Ziel-Eintrag
speichern. Mehrdeutigkeit wird ueber die Sinn-Ebene aufgeloest: 'Bank'
(Finanzinstitut) -> 'bank', 'Bank' (Sitzmoebel) -> 'bench' — niemals
ungeprueft Wort-Wort-Verknuepfungen.

Aufruf (Pipeline, rotiert mit den Übersetzungszielen):
  DICT_API_URL=... DICT_API_KEY=... MODEL_BASE_URL=... \
      python sense_translate.py --from de --to en --limit 100
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from harvester import api_get, api_post, post_chat_completions  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ.get(
    "MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")

PROMPT = (
    "Übersetze die folgenden Wörterbuch-BEDEUTUNGEN ins {ziel}. "
    "Achtung: Es geht um den konkreten Sinn, nicht das Stichwort. "
    "Mehrere gültige Übersetzungen sind erlaubt (keine Obergrenze)."
    " Ungefähre "
    "Entsprechungen bekommen ungefaehr=true — nichts erfinden: wenn "
    "keine Entsprechung existiert, leere Liste. Antworte NUR mit einem "
    'JSON-Objekt {"1":[{"wort":"...","ungefaehr":false}],"2":[],...}.'
    "\n\n"
)


def uebersetzungen_holen(model: str, ziel: str,
                         zeilen: list[str]) -> dict:
    nummeriert = "\n".join(f"{i + 1}. {z}" for i, z in enumerate(zeilen))
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(ziel=ziel) + nummeriert}],
        "max_tokens": 400,
        "temperature": 0,
    }
    # Request-Aufbau, Retry und 429-Fallback (Cloud-Quota) zentral in
    # harvester.post_chat_completions.
    try:
        r = post_chat_completions(body, int(os.environ.get('MODEL_TIMEOUT', '180')))
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
        eintraege = v if isinstance(v, list) else ([v] if v else [])
        liste = []
        for e in eintraege:
            if isinstance(e, dict) and e.get("wort"):
                liste.append({"wort": str(e["wort"]).strip().lower(),
                              "ungefaehr": bool(e.get("ungefaehr"))})
            elif isinstance(e, str) and e.strip() and e.strip() != "?":
                liste.append({"wort": e.strip().lower(),
                              "ungefaehr": False})
        if liste:
            erg[str(k)] = liste
    return erg


def sinn_einordnen(quell_sinn: dict, ziel_entry_id: int,
                   model: str) -> int | None:
    """Mehrdeutigen Zieleintrag semantisch auf einen Ziel-Sinn abbilden.

    Eine gleiche Wortart ist keine Bedeutungsübereinstimmung (Speise und
    Justizorgan sind beide Substantive). Das Modell sieht deshalb Glosse
    und Beschreibung des Quell-Sinns sowie alle Ziel-Sinne. Nur eine
    explizit gewählte, existierende Sinn-ID wird übernommen; ``null`` oder
    unklare Antworten lassen die Bestandsinformation offen.
    """
    try:
        sinne = api_get(f"/entries/{ziel_entry_id}/senses").get("senses", [])
    except Exception:
        return None
    if len(sinne) == 1:
        return sinne[0]["id"]
    if not sinne:
        return None
    kandidaten = [{
        "id": s["id"],
        "label": s.get("label") or "",
        "beschreibungen": s.get("definitions") or {},
    } for s in sinne]
    prompt = (
        "Ordne eine konkrete Wörterbuchbedeutung genau einer semantisch "
        "gleichen Zielbedeutung zu. Wortartgleichheit allein ist KEIN "
        "Beleg. Wenn keine Kandidatin eindeutig dieselbe Bedeutung hat, "
        'antworte mit {\"id\":null}. Sonst antworte ausschließlich mit '
        '{"id":ZAHL}.\nQuellbedeutung: '
        + json.dumps({
            "wort": quell_sinn.get("word") or "",
            "label": quell_sinn.get("label") or "",
            "beschreibung": quell_sinn.get("text") or "",
        }, ensure_ascii=False)
        + "\nZielbedeutungen: "
        + json.dumps(kandidaten, ensure_ascii=False)
    )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 60,
        "temperature": 0,
    }
    try:
        antwort = post_chat_completions(
            body, int(os.environ.get("MODEL_TIMEOUT", "180")))
        text = antwort["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*?\}", text, re.S)
        gewaehlt = json.loads(m.group(0)).get("id") if m else None
        gewaehlt = int(gewaehlt) if gewaehlt is not None else None
    except Exception:
        return None
    gueltig = {int(s["id"]) for s in sinne}
    return gewaehlt if gewaehlt in gueltig else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--model", default=os.environ.get(
        "MODEL_NAME", "mistral-large-4:cloud"))
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()
    frm, to = a.frm.lower(), a.to.lower()

    rows = api_get("/senses/translate-queue?" + urllib.parse.urlencode(
        {"from": frm, "to": to, "limit": a.limit})).get("rows", [])
    if not rows:
        print(f"{frm}->{to}: keine Sinne ohne Übersetzung")
        return
    print(f"{frm}->{to}: {len(rows)} Sinne zu übersetzen via {a.model}",
          flush=True)

    stat = {"verknuepft": 0, "fehlend": 0, "doppelt": 0, "mehrdeutig": 0}
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        zeilen = [f"{r['word']} ({r['label']}): {str(r.get('text') or '')[:200]}"
                  for r in chunk]
        uebers = uebersetzungen_holen(a.model, to, zeilen)
        for n, r in enumerate(chunk, 1):
            for ziel in uebers.get(str(n), []):
                try:
                    lu = api_get("/lookup?" + urllib.parse.urlencode(
                        {"word": ziel["wort"], "language": to}))
                except Exception:
                    stat["fehlend"] += 1
                    continue
                if not lu:
                    stat["fehlend"] += 1
                    continue
                body = {"entry_id": lu["id"],
                        "approx": ziel["ungefaehr"],
                        "source": "model"}
                try:
                    api_post(f"/senses/{r['sense_id']}/translations", body)
                    stat["verknuepft"] += 1
                    continue
                except urllib.error.HTTPError as e:
                    if e.code != 409:
                        stat["doppelt"] += 1
                        continue
                    # Zieleintrag mehrdeutig: konkrete Bedeutungen
                    # semantisch vergleichen; Wortart allein reicht nicht.
                    zid = sinn_einordnen(r, lu["id"], a.model)
                    if zid is None:
                        stat["mehrdeutig"] += 1
                        continue
                    body["target_sense_id"] = zid
                    try:
                        api_post(f"/senses/{r['sense_id']}/translations",
                                 body)
                        stat["verknuepft"] += 1
                    except Exception:
                        stat["doppelt"] += 1
                except Exception:
                    stat["doppelt"] += 1
        print(f"  {min(i + a.batch, len(rows))}/{len(rows)}: {stat}",
              flush=True)
        time.sleep(a.delay)
    print(f"{frm}->{to} fertig: {stat}")


if __name__ == "__main__":
    main()
