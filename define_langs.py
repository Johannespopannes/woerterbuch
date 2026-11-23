# -*- coding: utf-8 -*-
"""Bedeutungen in allen Sprachen nachreichen (ModelHub-Cloud/Jais).

Zwei Modi:
  own       Definition in der EINTRAGSSprache erzeugen (fehlt oft — der
            define-Harvester schreibt bisher Deutsch fuer alles).
  translate Definition in die Zielsprache uebersetzen (Quelle: bevorzugt
            Eintragssprache, dann 'de', dann 'en').

Aufruf (gleiche Umgebung wie die Ketten):
  MODEL_BASE_URL=... MODEL_NAME=mistral-large-4:cloud \
  DICT_API_URL=... DICT_API_KEY=... python define_langs.py \
      --mode own --language it --limit 2000

Idempotent: Eintraege mit vorhandener Definitions-Sprache werden uebersprungen
(Filter no_def=<lang> auf der API).
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

from harvester import LANG_NAMES, api_get, chat, parse_definitions  # noqa

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ["DICT_API_KEY"]

LANG_EN = {
    "de": "German", "en": "English", "es": "Spanish", "fr": "French",
    "it": "Italian", "ru": "Russian", "zh": "Chinese", "ja": "Japanese",
    "hi": "Hindi", "ar": "Arabic", "tn": "Tunisian Arabic (Derja)",
}

PROMPT_OWN = (
    "Definiere die folgenden {lang} Wörter — die Definitionen sollen "
    "AUSSCHLIESSLICH auf {lang_en} formuliert sein (Wortart auf {lang_en} "
    "plus Kernbedeutung, höchstens 15 Wörter, keine Beispielsätze). "
    "Fremdwörter: Herkunftssprache und Originalwort in Klammern. "
    "Antworte NUR mit einem JSON-Objekt, das jedes Wort auf seine "
    "{lang_en}e Definition abbildet, kein Markdown.\n"
    "Wörter: {words}"
)

PROMPT_TRANSLATE = (
    "Übersetze die folgenden Wortdefinitionen aus dem {src_en} ins "
    "{tgt_en}. Behalte das Format «Wortart: Bedeutung» bei (Wortart auf "
    "{tgt_en}), höchstens 15 Wörter pro Definition, keine Beispielsätze, "
    "keine Erklärung. Antworte NUR mit einem JSON-Objekt, das jedes Wort "
    "auf die {tgt_en}e Definition abbildet, kein Markdown.\n"
    "Definitionen: {defs}"
)

MAX_LEN = 1000


def put_def(entry_id: int, lang: str, text: str, source: str) -> bool:
    body = json.dumps({"lang": lang, "text": text,
                       "source": source}).encode()
    for versuch in range(4):
        req = urllib.request.Request(
            f"{DICT_API_URL}/entries/{entry_id}/definitions", data=body,
            headers={"Content-Type": "application/json",
                     "X-API-Key": DICT_API_KEY}, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                json.load(r)
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            print(f"  PUT {entry_id}/{lang}: HTTP {e} — "
                  f"Versuch {versuch + 1}/4", file=sys.stderr)
        except Exception as e:
            print(f"  PUT {entry_id}/{lang}: {e} — Versuch {versuch + 1}/4",
                  file=sys.stderr)
        time.sleep(15 * (versuch + 1))
    return False


def _snap(params: dict, limit: int) -> list[dict]:
    """Einträge seitenweise laden."""
    out, offset = [], 0
    while limit <= 0 or len(out) < limit:
        n = min(1000, limit - len(out)) if limit > 0 else 1000
        p = dict(params, limit=n, offset=offset)
        page = api_get("/entries?" + urllib.parse.urlencode(p))
        if not page:
            break
        out.extend(page)
        if len(page) < n:
            break
        offset += len(page)
    return out


def _json_map(out: str) -> dict:
    try:
        return parse_definitions(out)
    except Exception:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                return parse_definitions(m.group(0))
            except Exception:
                pass
    return {}


def run_own(language: str, limit: int, batch: int, delay: float) -> int:
    """Definitionen in der Eintragssprache erzeugen."""
    lang_en = LANG_EN.get(language, language)
    todo = _snap({"language": language, "no_def": language},
                 limit if limit > 0 else 0)
    print(f"own/{language}: {len(todo)} Einträge ohne {lang_en}-Definition",
          flush=True)
    total, fehler = 0, 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        words = ", ".join(e["word"] for e in chunk)
        try:
            out = chat(PROMPT_OWN.format(lang=LANG_NAMES.get(
                           language, language), lang_en=lang_en,
                           words=words), 0.3)
        except Exception as e:
            print(f"  Batch {i}: Modell-Fehler: {e}", file=sys.stderr)
            fehler += 1
            continue
        defs = _json_map(out)
        for e in chunk:
            d = defs.get(e["word"])
            if not d or not (2 <= len(d) <= MAX_LEN):
                continue
            if put_def(e["id"], language, d, "define-own"):
                total += 1
        print(f"  {i + len(chunk)}/{len(todo)}: +{total} gesamt",
              flush=True)
        time.sleep(delay)
    print(f"own/{language} fertig: {total} Definitionen, {fehler} Fehler")
    return 0 if not fehler else 2


def run_translate(target: str, limit: int, batch: int, delay: float,
                  langs: list[str]) -> int:
    """Definitionen in die Zielsprache übersetzen."""
    tgt_en = LANG_EN.get(target, target)
    todo = _snap({"no_def": target}, limit if limit > 0 else 0)
    print(f"translate->{target}: {len(todo)} Einträge ohne {tgt_en}-"
          f"Definition", flush=True)
    total, fehler = 0, 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        # Quell-Definitionen je Eintrag: eigene Sprache > de > en
        items = []
        for e in chunk:
            src = e.get("definition") if e.get("definition") else None
            src_lang = "de" if src else None
            if not src and e["word"]:
                for sl in (e["language"], "en"):
                    if sl == target or sl == src_lang:
                        continue
                    r = api_get(
                        f"/entries/{e['id']}/definitions")
                    cand = r.get("definitions", {}).get(sl)
                    if cand:
                        src, src_lang = cand, sl
                        break
            if src:
                items.append((e, src, src_lang))
        if not items:
            continue
        defs = "; ".join(f"{e['word']} ({se}): {s}"
                         for e, s, se in items)
        try:
            out = chat(PROMPT_TRANSLATE.format(
                src_en=LANG_EN.get(items[0][2], items[0][2]),
                tgt_en=tgt_en, defs=defs), 0.2)
        except Exception as ex:
            print(f"  Batch {i}: Modell-Fehler: {ex}", file=sys.stderr)
            fehler += 1
            continue
        m = _json_map(out)
        for e, _s, _sl in items:
            d = m.get(e["word"])
            if not d or not (2 <= len(d) <= MAX_LEN):
                continue
            if put_def(e["id"], target, d, f"translate"):
                total += 1
        print(f"  {i + len(chunk)}/{len(todo)}: +{total} gesamt",
              flush=True)
        time.sleep(delay)
    print(f"translate->{target} fertig: {total} Definitionen, "
          f"{fehler} Fehler")
    return 0 if not fehler else 2


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["own", "translate"], required=True)
    ap.add_argument("--language", help="Eintragssprache (mode=own)")
    ap.add_argument("--target", help="Zielsprache (mode=translate)")
    ap.add_argument("--limit", type=int, default=0, help="0 = alle")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()
    if a.mode == "own":
        if not a.language:
            ap.error("--mode own braucht --language")
        sys.exit(run_own(a.language.lower(), a.limit, a.batch, a.delay))
    if not a.target:
        ap.error("--mode translate braucht --target")
    sys.exit(run_translate(a.target.lower(), a.limit, a.batch, a.delay,
                           [a.target.lower()]))


if __name__ == "__main__":
    main()
