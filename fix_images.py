"""Nachbesserungs-Pipeline für schlecht bewertete Wort-Bilder.

Ablauf je Eintrag (aus GET /raten/schlecht):
1. Aktuelles Bild vom Server laden
2. Gemma 4 (Vision, lokal :8082) analysiert, was zu sehen ist, und
   formuliert einen besseren englischen Bildprompt
3. Neu rendern (sd-cli, neuer Seed), hochladen — neuer Dateiname mit
   Versionszähler, damit Browser-Caches nicht das alte Bild zeigen

Beispiel:
    python fix_images.py --min-votes 2
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

from image_harvester import CACHE_DIR, api_upload_image, render

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ.get("DICT_API_KEY", "")
GEMMA_URL = os.environ.get("GEMMA_URL", "http://127.0.0.1:8082/v1")

CRITIQUE_PROMPT = (
    'Dieses Bild soll das Wort "{word}" als einfaches Piktogramm darstellen '
    '(Bedeutung: "{definition}"). Menschen haben es als NICHT verständlich '
    'bewertet.\n'
    '1) Was ist wirklich zu sehen? 2) Warum erkennt man "{word}" nicht?\n'
    'Antworte danach mit GENAU einer Zeile im Format:\n'
    'PROMPT: <besserer englischer Bildprompt, höchstens 15 Wörter, flaches '
    'Piktogramm, weißer Hintergrund>'
)


def api_get(path: str):
    headers = {}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(f"{DICT_API_URL}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def download_image(filename: str) -> bytes:
    with urllib.request.urlopen(
            f"{DICT_API_URL}/bilder/{filename}", timeout=60) as resp:
        return resp.read()


def gemma_better_prompt(word: str, definition: str, png: bytes) -> str | None:
    img = base64.b64encode(png).decode()
    body = {
        "model": "gemma-vision",
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + img}},
            {"type": "text", "text": CRITIQUE_PROMPT.format(
                word=word, definition=(definition or "").strip()[:200])},
        ]}],
        "max_tokens": 1000,  # Reasoning-Modell: Denkpfad braucht Budget
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        f"{GEMMA_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=300))
    except Exception as e:
        print(f"  Gemma-Fehler: {e}", file=sys.stderr)
        return None
    content = r["choices"][0]["message"].get("content") or ""
    for line in content.splitlines():
        line = line.strip()
        if line.upper().startswith("PROMPT:"):
            return line.split(":", 1)[1].strip() or None
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-votes", type=int, default=2)
    ap.add_argument("--size", type=int, default=384)
    ap.add_argument("--steps", type=int, default=16)
    ap.add_argument("--delay", type=float, default=0.2)
    args = ap.parse_args()

    entries = api_get(f"/raten/schlecht?min_votes={args.min_votes}")
    print(f"{len(entries)} schlecht bewertete Bilder")
    fixed = failed = 0
    for e in entries:
        print(f"[{e['language']}] {e['word']} (ja {e['ja']} / nein {e['nein']})")
        try:
            png = download_image(e["image"])
        except Exception as ex:
            print(f"  Bild nicht ladbar: {ex}", file=sys.stderr)
            failed += 1
            continue
        prompt = gemma_better_prompt(e["word"], e["definition"] or "", png)
        if not prompt:
            print("  kein neuer Prompt von Gemma", file=sys.stderr)
            failed += 1
            continue
        print(f"  neuer Prompt: {prompt}")
        version = sum(1 for p in CACHE_DIR.glob(
            f"{e['language']}-{e['id']}-v*.png")) + 2
        filename = f"{e['language']}-{e['id']}-v{version}.png"
        out = CACHE_DIR / filename
        seed = (e["id"] * 7919 + version) % 2**31
        if not render(prompt, out, args.size, args.steps, seed):
            print("  Render-Fehler", file=sys.stderr)
            failed += 1
            continue
        try:
            api_upload_image(e["id"], filename, out.read_bytes())
            fixed += 1
        except Exception as ex:
            print(f"  Upload-Fehler: {ex}", file=sys.stderr)
            failed += 1
        time.sleep(args.delay)
    print(f"fertig: {fixed} Bilder ersetzt, {failed} Fehler")


if __name__ == "__main__":
    main()
