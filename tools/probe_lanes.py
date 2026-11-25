# -*- coding: utf-8 -*-
"""Sequentielle Probe aller Laptop-Lanes durchs ModelHub-Gateway.

Je Lane EIN echter Harvester-Prompt (_free_prompt, 20 Wörter), Dauer +
Ausbeute werden gemessen. Aufruf: python tools/probe_lanes.py
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

# (Lane-ID im ModelHub, Sprachcode, Buchstabe f. Prompt)
LANES = [
    ("fantastica-7b:laptop", "it", "a"),
    ("llm-jp-4.1-32b:laptop", "ja", "あ"),
    ("t-pro-it-2.0:laptop", "ru", "а"),
    ("yi-1.5-34b:laptop", "zh", ""),
    ("fanar-2-27b:laptop", "ar", "ا"),
]

def chat_stream(prompt: str) -> str:
    """Streaming-Variante (Bug B: Non-Stream instabil bei langsamen Lanes).

    SSE auswerten, delta.content anhängen; reasoning wird ignoriert."""
    import urllib.request
    body = json.dumps({
        "model": H.MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 8000,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{H.MODEL_BASE_URL}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {H.MODEL_API_KEY}"})
    out = []
    with urllib.request.urlopen(req, timeout=int(os.environ.get(
            "MODEL_TIMEOUT", "900"))) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            for ch in d.get("choices", []):
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    out.append(delta["content"])
    return "".join(out)


def probe(lane, lang, letter):
    """Ein echter Harvester-Prompt; 429/502/leer → bis zu 5x, 90 s Abstand.

    Thinking-Lanes (llm-jp, t-pro-it) direkt per Stream (Bug B)."""
    langname = H.LANG_NAMES.get(lang, lang)
    prompt = H._free_prompt(langname, lang, letter, 20)
    use_stream = lang in ("ja", "ru")
    for versuch in range(1, 6):
        t0 = time.time()
        try:
            raw = chat_stream(prompt) if use_stream else H.chat(prompt, 0.7)
            if not raw.strip():
                raise ValueError("leere Antwort")
            dur = time.time() - t0
            words = H.parse_words(raw)
            ok = [w for w in words if H._matches_letter(w, letter, lang)]
            res = {"lane": lane, "lang": lang, "sec": round(dur, 1),
                   "stream": use_stream,
                   "words": len(words), "letter_ok": len(ok),
                   "sample": ok[:8]}
            if use_stream:
                res["via"] = "stream"
            return res
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}"
            use_stream = True   # nach HTTP-Fehler ebenfalls Stream versuchen
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
        print(f"  [{lane}] Versuch {versuch}: {detail} — warte 90 s",
              flush=True)
        time.sleep(90)
    return {"lane": lane, "lang": lang, "error": detail}


results = []
for lane, lang, letter in LANES:
    H.MODEL_NAME = lane
    res = probe(lane, lang, letter)
    results.append(res)
    print(json.dumps(res, ensure_ascii=False), flush=True)
    time.sleep(30)   # Abstand zwischen den Lanes

print("\n=== ZUSAMMENFASSUNG ===")
for r in results:
    if "error" in r:
        print(f"{r['lane']:28s} FEHLER: {r['error']}")
    else:
        print(f"{r['lane']:28s} {r['sec']:7.1f}s  {r['words']:3d} Wörter "
              f"({r['letter_ok']} im Buchstaben)  {r['sample'][:4]}")
