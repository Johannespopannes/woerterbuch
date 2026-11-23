# -*- coding: utf-8 -*-
"""Autonome Herkunftsbelegung der Sinne (Wortgeschichte mit Nachweis).

Fuer jeden Sinn ohne Herkunftsbeleg:
  1. Wiktionary-Wikitext des Worts holen (MediaWiki parse-API)
  2. Etymologie-Abschnitt(e) extrahieren (Herkunft/Etymologie/Etymology/
     Étymologie/Origine/Etimología/词源/語源/…)
  3. Modell prueft: stuetzt der Abschnitt eine konkrete Herkunftsaussage
     fuer DIESEN Sinn? Antwort als {'belegt': bool, 'aussage': str}
  4. Nur bei belegt=true wird geschrieben: sense_links(kind='etymologie',
     url=Wiktionary-Seite, claim=Aussage, source='verify:wiktionary')

Nichts wird geraten: ohne Abschnitt oder ohne Beleg bleibt der Sinn
unbelegt und kommt naechste Runde wieder (Verbesserungen im Wiki
werden so automatisch uebernommen).

Aufruf (Pipeline, je Sprachkette):
  DICT_API_URL=... DICT_API_KEY=... MODEL_BASE_URL=... \
      python sense_etym.py --language de --limit 80 --batch 5
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

from harvester import api_get, api_post  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]
MODEL_BASE_URL = os.environ.get(
    "MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")

UA = {"User-Agent": "woerterbuch-tassekaffee-etym/1.0 "
                    "(woerterbuch@tassekaffee.com)"}
# Etymologie-Überschriften in den Wiktionary-Sprachausgaben
ETYM_KOPF = re.compile(
    r"^={2,6}\s*(Herkunft|Etymologie|Etymology|Étymologie|Origine|"
    r"Etimolog\w*|Origen|Etymologia|Etymolog\w*|词源|語源|어원|"
    r"Etymolog[iy]\w*)\s*={2,6}\s*$",
    re.I | re.M)
KOPF_ZEILE = re.compile(r"^={2,6}[^=]", re.M)

PROMPT = (
    "Du prüfst eine Herkunftsbehauptung für ein Wörterbuch. Gegeben: "
    'Wort "{wort}" ({sprache}), Sinn: {sinn}. Unten steht der '
    "Etymologie-Abschnitt aus dem Wiktionary. Beantworte NUR mit JSON "
    '{{"belegt": true/false, "aussage": "..."}} — belegt=true NUR wenn '
    "der Text eine konkrete Herkunftsaussage für dieses Wort enthält "
    "(Ursprungssprache, Wortstamm, erste Belege o. ä.). aussage = EINE "
    "kurze deutsche Aussage, die wörtlich im Text steht (nichts "
    "ergänzen). Kein Beleg → belegt=false, aussage leer.\n\n"
    "ETYMOLOGIE-ABSCHNITT:\n{ausschnitt}\n"
)


def wikitext_holen(language: str, word: str) -> str | None:
    api = f"https://{language}.wiktionary.org/w/api.php"
    qs = urllib.parse.urlencode({
        "action": "parse", "format": "json", "prop": "wikitext",
        "redirects": "1", "page": word})
    req = urllib.request.Request(api + "?" + qs, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"  Wiki-API {word}: {e}", file=sys.stderr)
        return None
    if "error" in data:
        return None
    try:
        return data["parse"]["wikitext"]["*"]
    except Exception:
        return None


def etym_abschnitte(wikitext: str, max_len: int = 1800) -> str:
    """Alle Etymologie-Abschnitte hintereinander, grob bereinigt."""
    teile = []
    for m in ETYM_KOPF.finditer(wikitext):
        naechster = KOPF_ZEILE.search(wikitext, m.end())
        ende = naechster.start() if naechster else len(wikitext)
        teile.append(wikitext[m.end():ende])
    roh = "\n".join(t.strip() for t in teile if t.strip())
    # Wikitext-Syntax grob reduzieren (Templates, Links, Formatierung)
    txt = re.sub(r"\{\{[^{}]*\}\}", " ", roh)
    txt = re.sub(r"\{\{[^{}]*\}\}", " ", txt)
    txt = re.sub(r"\[\[([^]|]*\|)?([^]]*)\]\]", r"\2", txt)
    txt = re.sub(r"'{2,}", "", txt)
    txt = re.sub(r"<ref[^>]*>.*?</ref>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len]


def modell_pruefen(model: str, batch: list[dict],
                   ausschnitte: dict[int, str]) -> dict:
    """Ein gemeinsamer Aufruf pro Batch: {sense_id: aussage}"""
    ids = [r["sense_id"] for r in batch if ausschnitte.get(r["sense_id"])]
    nummeriert = []
    n = 0
    for r in batch:
        a = ausschnitte.get(r["sense_id"])
        if not a:
            continue
        n += 1
        nummeriert.append(
            f"### AUFGABE {n} ###\n" + PROMPT.format(
                wort=r["word"], sprache=r["language"],
                sinn=r["label"], ausschnitt=a))
    if not nummeriert:
        return {}
    body = {
        "model": model,
        "messages": [{"role": "user", "content":
                      "Prüfe jede der folgenden "
                      f"{len(nummeriert)} Aufgaben. Antworte NUR mit "
                      'einem JSON-Objekt {"1": {"belegt": ..., '
                      '"aussage": ...}, "2": {...}, ...}.\n\n'
                      + "\n\n".join(nummeriert)}],
        "max_tokens": 1200,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{MODEL_BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MODEL_API_KEY}"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=240))
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
    for n, sid in enumerate(ids, 1):
        v = roh.get(str(n))
        if isinstance(v, dict) and v.get("belegt") is True and v.get("aussage"):
            erg[sid] = str(v["aussage"]).strip()[:190]
    return erg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--language", required=True)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--model", default=os.environ.get(
        "MODEL_NAME", "mistral-large-4:cloud"))
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()
    lang = a.language.lower()

    rows = api_get(f"/senses/etym-queue?language={lang}&limit={a.limit}"
                   ).get("rows", [])
    if not rows:
        print(f"{lang}: Herkunftsbelegung — nichts zu tun")
        return
    print(f"{lang}: {len(rows)} Sinne ohne Herkunftsbeleg", flush=True)

    # Phase 1: Wiktionary-Abschnitte sammeln
    ausschnitte = {}
    for r in rows:
        wt = wikitext_holen(lang, r["word"])
        if wt:
            ab = etym_abschnitte(wt)
            if ab:
                ausschnitte[r["sense_id"]] = ab
        time.sleep(a.delay)
    print(f"  Etymologie-Abschnitte gefunden: {len(ausschnitte)}/{len(rows)}",
          flush=True)

    # Phase 2+3: prüfen und belegen
    stat = {"belegt": 0, "ohne_beleg": 0}
    seite = "https://{}.wiktionary.org/wiki/{}"
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        pruefungen = modell_pruefen(a.model, chunk, ausschnitte)
        for r in chunk:
            sid = r["sense_id"]
            if sid not in ausschnitte:
                stat["ohne_beleg"] += 1
                continue
            aussage = pruefungen.get(sid)
            if not aussage:
                stat["ohne_beleg"] += 1
                continue
            api_post(f"/senses/{sid}/links", {
                "kind": "etymologie",
                "url": seite.format(lang, urllib.parse.quote(r["word"])),
                "claim": aussage,
                "source": "verify:wiktionary"})
            stat["belegt"] += 1
        print(f"  {min(i + a.batch, len(rows))}/{len(rows)}: {stat}",
              flush=True)
        time.sleep(a.delay)
    print(f"{lang} fertig: {stat}")


if __name__ == "__main__":
    main()
