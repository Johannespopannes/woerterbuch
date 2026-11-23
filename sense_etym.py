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
# Etymologie-Überschriften in den Wiktionary-Sprachausgaben (auf den
# RENDERTITELN gematcht — die parse-API loest {{Sprache|Deutsch}} zu
# "Deutsch" auf, {{Herkunft}} zu "Herkunft")
ETYM_TITEL = re.compile(
    r"(Herkunft|Etymologie|Etymology|Étymologie|Origine|Etimolog\w*|"
    r"Origen|Etymolog\w*|词源|語源|语源|어원)",
    re.I)


def _api_json(language: str, params: dict) -> dict:
    api = f"https://{language}.wiktionary.org/w/api.php"
    qs = urllib.parse.urlencode(dict(params, format="json"))
    req = urllib.request.Request(api + "?" + qs, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def etym_texte_holen(language: str, word: str,
                     max_len: int = 1800) -> list[str]:
    """Gerenderte Etymologie-Abschnitte eines Artikels (bereinigter Text).

    Zwei Schritte: Abschnittsliste mit aufgeloesten Titeln, dann jeder
    Abschnitt einzeln als gerendertes HTML — Templates sind dort bereits
    zu Woertern aufgeloest ('inherited from Middle English bank'),
    Wikitext-Regexerei entfaellt.
    """
    try:
        data = _api_json(language, {
            "action": "parse", "prop": "sections", "redirects": "1",
            "page": word})
    except Exception as e:
        print(f"  Wiki-API {word}: {e}", file=sys.stderr)
        return []
    if "error" in data:
        return []
    treffer = [s for s in data.get("parse", {}).get("sections", [])
               if ETYM_TITEL.search(str(s.get("line", "")))]
    texte = []
    for s in treffer[:3]:
        try:
            html = _api_json(language, {
                "action": "parse", "prop": "text", "redirects": "1",
                "page": word, "section": s["index"]})
            fragment = html["parse"]["text"]["*"]
        except Exception:
            continue
        txt = re.sub(r"<ref[^>]*>.*?</ref>", " ", fragment,
                     flags=re.S | re.I)
        txt = re.sub(r"<style[^>]*>.*?</style>", " ", txt,
                     flags=re.S | re.I)
        txt = re.sub(r"<[^>]+>", " ", txt)
        import html as html_mod
        txt = html_mod.unescape(txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        # Rendered Abschnitte tragen den Seiten-TOC am Anfang; der echte
        # Abschnittskoerper beginnt beim ersten Bearbeiten-Link.
        bearb = re.search(
            r"\[\s*(edit|modifier|modifica|bearbeiten|redigera|"
            r"uredi|bewerk|editar)", txt, re.I)
        if bearb and bearb.start() < 1500:
            txt = txt[bearb.start():]
        txt = re.sub(r"\[\s*(edit|modifier[^]]*|modifica[^]]*|"
                     r"bearbeiten|redigera|uredi|bewerk|editar)[^]]*\]",
                     " ", txt, flags=re.I)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            texte.append(txt[:max_len])
    if texte:
        return texte
    # Fallback deutsches Wiktionary: {{Herkunft}}-Block im Artikelkopf
    # (keine Section). Die Sprachbausteine {{gmh.}}/{{goh.}} bleiben
    # stehen — das Modell liest Wikitext-Etymologie zuverlaessig.
    try:
        wt_data = _api_json(language, {
            "action": "parse", "prop": "wikitext", "redirects": "1",
            "page": word})
        wt = wt_data["parse"]["wikitext"]["*"]
    except Exception:
        return []
    m = re.search(r"^\{\{Herkunft[^}]*\}\}\s*$", wt, re.M)
    if not m:
        return []
    zeilen = []
    for ln in wt[m.end():].splitlines():
        if ln.startswith(":"):
            zeilen.append(ln.lstrip(":").strip())
        elif ln.strip() == "":
            continue
        else:
            break
    roh = " ".join(z for z in zeilen if z)
    roh = re.sub(r"<ref[^>]*>.*?</ref>", " ", roh, flags=re.S | re.I)
    roh = re.sub(r"<ref[^>]*/>", " ", roh)
    roh = re.sub(r"\s+", " ", roh).strip()
    return [roh[:max_len]] if roh else []

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

    # Phase 1: gerenderte Etymologie-Abschnitte sammeln
    ausschnitte = {}
    for r in rows:
        texte = etym_texte_holen(lang, r["word"])
        if texte:
            ausschnitte[r["sense_id"]] = "\n---\n".join(texte)
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
