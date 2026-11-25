# -*- coding: utf-8 -*-
"""Runde 2: Proben der restlichen Spezialisten-Lanes (ko tr th bg uk es).

Sequentiell, ein Request je Lane, Retry bei 429/502/leer (90 s Abstand).
th/es direkt per Stream (Bug B: siamgpt/alia Non-Stream instabil).
Aufruf: python tools/probe_lanes_round2.py
"""
import os
import sys
import time
import json
import urllib.error

os.environ.setdefault("MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")
os.environ.setdefault("MODEL_API_KEY", "dummy")
os.environ.setdefault("MODEL_TIMEOUT", "900")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harvester as H
from probe_lanes import chat_stream

# (Lane-ID, Sprachcode, Buchstabe f. Prompt, direkt per Stream)
LANES = [
    ("bggpt-gemma-3-27b:laptop", "bg", "а", False),
    ("lapa-12b:laptop", "uk", "а", False),
    ("turkish-llm-32b:laptop", "tr", "a", False),
    ("exaone-3.5-32b:laptop", "ko", "ㄱ", False),
    ("siamgpt-32b:laptop", "th", "ก", True),
    ("alia-40b:laptop", "es", "a", True),
]


def probe(lane, lang, letter, use_stream):
    langname = H.LANG_NAMES.get(lang, f"{language_name(lang)}")
    prompt = H._free_prompt(langname, lang, letter, 15)
    detail = ""
    for versuch in range(1, 6):
        t0 = time.time()
        try:
            raw = chat_stream(prompt) if use_stream else H.chat(prompt, 0.7)
            if not raw.strip():
                raise ValueError("leere Antwort")
            dur = time.time() - t0
            words = H.parse_words(raw)
            return {"lane": lane, "lang": lang, "sec": round(dur, 1),
                    "stream": use_stream, "words": len(words),
                    "sample": words[:10]}
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}"
            use_stream = True
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
        print(f"  [{lane}] Versuch {versuch}: {detail} — warte 90 s",
              flush=True)
        time.sleep(90)
    return {"lane": lane, "lang": lang, "error": detail}


def language_name(code):
    return {"ko": "Koreanisch", "tr": "Türkisch", "th": "Thailändisch",
            "bg": "Bulgarisch", "uk": "Ukrainisch", "es": "Spanisch"}.get(
                code, code)


results = []
for lane, lang, letter, stream in LANES:
    H.MODEL_NAME = lane
    res = probe(lane, lang, letter, stream)
    results.append(res)
    print(json.dumps(res, ensure_ascii=False), flush=True)
    time.sleep(30)

print("\n=== ZUSAMMENFASSUNG ===")
for r in results:
    if "error" in r:
        print(f"{r['lane']:30s} FEHLER: {r['error']}")
    else:
        print(f"{r['lane']:30s} {r['sec']:7.1f}s  {r['words']:3d} Wörter  "
              f"{r['sample'][:5]}")
