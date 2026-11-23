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


def _block_aus_wikitext(wt: str, vorlage: str, max_len: int) -> str:
    """{{Vorlage}}-Block (Zeilen ab ':') aus dem Artikelkopf extrahieren."""
    m = re.search(r"^\{\{" + vorlage + r"[^}]*\}\}\s*$", wt, re.M)
    if not m:
        return ""
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
    return roh[:max_len]


def dwds_text_holen(word: str, max_len: int = 1500) -> str:
    """Herkunfts-Absatz der DWDS-Wortseite (deutsch)."""
    url = "https://www.dwds.de/wb/" + urllib.parse.quote(word)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            seite = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    m = re.search(r"<h[23][^>]*>\s*Herkunft\s*</h[23]>(.*?)"
                  r"<h[23][^>]*>", seite, re.S | re.I)
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    import html as html_mod
    txt = html_mod.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len]


def beleg_texte_holen(language: str, word: str,
                      max_len: int = 1800) -> list[tuple[str, str]]:
    """(text, quelle)-Tupel: wiktionary-Herkunft, wiktionary-Wortbildung
    (de), DWDS-Herkunft (de)."""
    texte: list[tuple[str, str]] = []
    try:
        data = _api_json(language, {
            "action": "parse", "prop": "sections", "redirects": "1",
            "page": word})
    except Exception as e:
        print(f"  Wiki-API {word}: {e}", file=sys.stderr)
        data = {}
    if "parse" in data:
        treffer = [s for s in data["parse"].get("sections", [])
                   if ETYM_TITEL.search(str(s.get("line", "")))]
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
                texte.append((txt[:max_len], "wiktionary"))
    if texte:
        return texte
    # Fallback deutsches Wiktionary: {{Herkunft}}- und {{Wortbildung}}-
    # Bloecke im Artikelkopf (keine Sections).
    try:
        wt = _api_json(language, {
            "action": "parse", "prop": "wikitext", "redirects": "1",
            "page": word})["parse"]["wikitext"]["*"]
    except Exception:
        wt = ""
    if wt:
        h = _block_aus_wikitext(wt, "Herkunft", max_len)
        if h:
            texte.append((h, "wiktionary"))
        w = _block_aus_wikitext(wt, "Wortbildung", max_len)
        if w:
            texte.append((w, "wiktionary-wortbildung"))
    # DWDS nur fuer deutsche Woerter
    if language == "de":
        d = dwds_text_holen(word)
        if d:
            texte.append((d, "dwds"))
    return texte


PROMPT = (
    "Du prüfst Herkunfts-/Wortbildungs-Behauptungen für ein Wörterbuch. "
    'Gegeben: Wort "{wort}" ({sprache}), Sinn: {sinn}. Unten steht ein '
    "Belegtext (Wiktionary oder DWDS). Beantworte NUR mit JSON "
    '{{"belegt": true/false, "aussage": "...", "typ": "herkunft" | '
    '"wortbildung", "unsicher": true/false}} — belegt=true NUR wenn der '
    "Text eine konkrete Aussage enthält: typ=herkunft (Ursprungssprache, "
    "Wortstamm, Entlehnung, erste Belege, Bedeutungsentwicklung) oder "
    "typ=wortbildung (Zusammensetzung/Ableitung wie 'aus Motor und "
    "Haube zusammengesetzt'). aussage = EINE kurze deutsche Aussage, "
    "die im Text steht (nichts ergänzen). unsicher=true wenn der Text "
    "die Aussage selbst als Vermutung kennzeichnet ('vermutlich', "
    "'wohl', 'unklar', 'vielleicht'). Kein Beleg → belegt=false.\n\n"
    "BELEGTEXT:\n{ausschnitt}\n"
)


def modell_pruefen(model: str, batch: list[dict],
                   ausschnitte: dict[int, str]) -> dict:
    """Ein gemeinsamer Aufruf pro Batch: {sense_id: (aussage, typ, unsicher)}"""
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
                      '"aussage": ..., "typ": ..., "unsicher": ...}, '
                      '"2": {...}, ...}.\n\n'
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
            erg[sid] = (str(v["aussage"]).strip()[:190],
                        str(v.get("typ", "herkunft")).strip().lower(),
                        bool(v.get("unsicher")))
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

    # Phase 1: Belegtexte sammeln (wiktionary-Herkunft/-Wortbildung, DWDS)
    ausschnitte = {}
    quellen = {}
    for r in rows:
        texte = beleg_texte_holen(lang, r["word"])
        if texte:
            ausschnitte[r["sense_id"]] = "\n---\n".join(t for t, _ in texte)
            quellen[r["sense_id"]] = [q for _, q in texte]
        time.sleep(a.delay)
    print(f"  Belegtexte gefunden: {len(ausschnitte)}/{len(rows)}",
          flush=True)

    # Phase 2+3: prüfen und belegen
    stat = {"belegt": 0, "wortbildung": 0, "vermutung": 0, "ohne_beleg": 0}
    seite = "https://{}.wiktionary.org/wiki/{}"
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        pruefungen = modell_pruefen(a.model, chunk, ausschnitte)
        for r in chunk:
            sid = r["sense_id"]
            if sid not in ausschnitte:
                stat["ohne_beleg"] += 1
                continue
            pruefung = pruefungen.get(sid)
            if not pruefung:
                stat["ohne_beleg"] += 1
                continue
            aussage, typ, unsicher = pruefung
            # Wikitext-Reste aus der Aussage entfernen ([[x|y]] -> y)
            aussage = re.sub(r"\[\[([^]|]*\|)?([^]]*)\]\]", r"\2", aussage)
            aussage = aussage.replace("''", "").strip()
            if not aussage:
                stat["ohne_beleg"] += 1
                continue
            wortbildung = typ == "wortbildung"
            # Belegquelle: Wortbildung-Abschnitt → wiktionary, sonst erste
            # Quelle des Sinns (wiktionary oder dwds)
            qs = quellen.get(sid) or ["wiktionary"]
            if wortbildung:
                beleg = "wiktionary"
            else:
                beleg = "dwds" if "dwds" in qs else qs[0]
            api_post(f"/senses/{sid}/links", {
                "kind": "wortbildung" if wortbildung else "etymologie",
                "url": seite.format(lang, urllib.parse.quote(r["word"])),
                "claim": aussage,
                "source": ("vermutung:" if unsicher else "verify:") + beleg})
            stat["belegt"] += 1
            stat["wortbildung"] += 1 if wortbildung else 0
            stat["vermutung"] += 1 if unsicher else 0
        print(f"  {min(i + a.batch, len(rows))}/{len(rows)}: {stat}",
              flush=True)
        time.sleep(a.delay)
    print(f"{lang} fertig: {stat}")


if __name__ == "__main__":
    main()
