# -*- coding: utf-8 -*-
"""Gepruefte Externquellen-Links fuer Eintraege ermitteln (Wiktionary,
Wikipedia, DWDS) und ueber die API speichern.

Pruefung per MediaWiki-Batch-API (bis 40 Titel pro Aufruf): Nur Eintraege,
die es WIRKLICH gibt, werden geschrieben — der Quellenblock auf der
Wortseite bleibt damit frei von Sackgassen. Wikipedia ist dabei
weiterfuehrende Sachquelle, kein etymologischer Nachweis.

Aufruf:
  DICT_API_URL=... DICT_API_KEY=... python source_links.py \
      --language it --limit 1000
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from harvester import api_get  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]

UA = {"User-Agent": "woerterbuch-tassekaffee-sourcecheck/1.0 "
                    "(woerterbuch@tassekaffee.com)"}
# tn hat kein eigenes Wiktionary/Wikipedia — keine Wiki-Pruefung.
WIKIS = ["wiktionary", "wikipedia"]


def post_link(entry_id: int, kind: str, url: str) -> bool:
    body = json.dumps({"kind": kind, "url": url}).encode()
    for versuch in range(3):
        req = urllib.request.Request(
            f"{DICT_API_URL}/entries/{entry_id}/source-links", data=body,
            headers={"Content-Type": "application/json",
                     "X-API-Key": DICT_API_KEY}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                json.load(r)
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            print(f"  POST {entry_id}/{kind}: HTTP {e}", file=sys.stderr)
        except Exception as e:
            print(f"  POST {entry_id}/{kind}: {e}", file=sys.stderr)
        time.sleep(10 * (versuch + 1))
    return False


def wiki_check(language: str, wiki: str, titles: list[str]) -> dict:
    """Titel gegen ein Wiki pruefen. Liefert {titel: url} fuer vorhandene."""
    found = {}
    api = f"https://{language}.{wiki}.org/w/api.php"
    for i in range(0, len(titles), 40):
        chunk = titles[i:i + 40]
        qs = urllib.parse.urlencode({
            "action": "query", "format": "json", "prop": "info",
            "inprop": "url", "redirects": "1",
            "titles": "|".join(chunk)})
        req = urllib.request.Request(api + "?" + qs, headers=UA)
        for versuch in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                for p in data.get("query", {}).get("pages", {}).values():
                    if "missing" in p or "fullurl" not in p:
                        continue
                    t = p.get("title", "")
                    if t in chunk:
                        found[t] = p["fullurl"]
                break
            except Exception as e:
                if versuch == 2:
                    print(f"  {wiki}-API-Fehler: {e}", file=sys.stderr)
                time.sleep(5 * (versuch + 1))
        time.sleep(0.5)
    return found


def dwds_check(words: list[str]) -> dict:
    """DWDS-Woerterbuchpfade pruefen (nur fuer de sinnvoll)."""
    found = {}
    for w in words:
        url = "https://www.dwds.de/wb/" + urllib.parse.quote(w)
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    found[w] = url
        except Exception:
            pass
        time.sleep(0.3)
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--language", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = alle")
    ap.add_argument("--delay", type=float, default=0.2)
    a = ap.parse_args()
    lang = a.language.lower()

    out, offset = [], 0
    while True:
        n = min(1000, a.limit - len(out)) if a.limit > 0 else 1000
        if n <= 0:
            break
        page = api_get("/entries?" + urllib.parse.urlencode(
            {"language": lang, "nolinks": "true", "limit": n,
             "offset": offset}))
        if not page:
            break
        out.extend(page)
        if len(page) < n:
            break
        offset += len(page)
        if a.limit > 0 and len(out) >= a.limit:
            break

    print(f"{lang}: {len(out)} Einträge mit offenen Quellen", flush=True)

    total = 0
    for i in range(0, len(out), 40):
        chunk = out[i:i + 40]
        titles = [e["word"] for e in chunk]
        for wiki in WIKIS:
            gefunden = wiki_check(lang, wiki, titles)
            for e in chunk:
                url = gefunden.get(e["word"])
                if url and post_link(e["id"], wiki, url):
                    total += 1
        if lang == "de":
            g = dwds_check(titles)
            for e in chunk:
                if e["word"] in g and post_link(e["id"], "dwds",
                                                g[e["word"]]):
                    total += 1
        print(f"  {min(i + 40, len(out))}/{len(out)}: {total} Links "
              f"gesamt", flush=True)
        time.sleep(a.delay)
    print(f"{lang} fertig: {total} gepruefte Links geschrieben")


if __name__ == "__main__":
    main()
