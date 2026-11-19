"""Bild-Pipeline: Piktogramme für Wörterbuch-Einträge.

Ablauf je Sprache:
1. Einträge ohne Bild holen (API-Filter noimage=true)
2. Mistral übersetzt Wörter portionsweise in kurze englische Bildmotive
   (SD 1.5 kann Deutsch kaum; ein LLM-Batch pro 50 Wörter)
3. sd-cli (stable-diffusion.cpp, Vulkan) rendert das Piktogramm
4. Upload per POST /entries/{id}/image (Base64), lokaler Cache bleibt

Beispiel:
    python image_harvester.py --language de --limit 50
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from harvester import LANG_NAMES, api_get, chat, parse_definitions

SD_CLI = os.environ.get(
    "SD_CLI", r"C:\Users\johan\Bonsai-demo\bin\sd-vulkan\sd-cli.exe")
SD_MODEL = os.environ.get(
    "SD_MODEL", r"C:\Models\image\sd15\v1-5-pruned-emaonly.safetensors")
LORA_DIR = os.environ.get("SD_LORA_DIR", r"C:\Models\image\lora")
CACHE_DIR = Path(os.environ.get(
    "IMAGE_CACHE", r"C:\Repos\_scratch\woerterbuch-chain\bilder-cache"))

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ.get("DICT_API_KEY", "")

PROMPT_TEMPLATE = (
    "flat vector pictogram of {concept}, <lora:IconsRedmond15V-Icons:0.9>, "
    "minimal geometric icon, solid colors, centered, isolated on pure white "
    "background, no text"
)
NEGATIVE = "photo, realistic, text, watermark, blurry, complex background"

TRANSLATE_PROMPT = (
    "Gib für jedes der folgenden {lang} Wörter ein kurzes, konkretes "
    "englisches Bildmotiv für ein Piktogramm an (1-3 englische Wörter, "
    "z. B. Haus -> house, laufen -> person running, Freiheit -> bird flying "
    "free). Abstrakte Wörter auf ein sichtbares Symbol bringen. "
    "Antworte NUR mit einem JSON-Objekt Wort->Motiv, kein Markdown.\n"
    "Wörter: {words}"
)


def api_upload_image(entry_id: int, filename: str, data: bytes) -> None:
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}/entries/{entry_id}/image",
        data=json.dumps({
            "filename": filename,
            "data_b64": base64.b64encode(data).decode(),
        }).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def translate_batch(words: list[str], lang: str) -> dict[str, str]:
    """Wörter -> englische Bildmotive (ein LLM-Aufruf pro Liste)."""
    try:
        out = parse_definitions(chat(
            TRANSLATE_PROMPT.format(lang=lang, words=", ".join(words)), 0.3))
        return {w: str(out.get(w, "")).strip() for w in words if out.get(w)}
    except Exception as e:
        print(f"  Übersetzungs-Batch fehlgeschlagen: {e}", file=sys.stderr)
        return {}


def render(concept: str, out_path: Path, size: int, steps: int,
           seed: int) -> bool:
    """Ein Piktogramm rendern. True bei Erfolg."""
    cmd = [
        SD_CLI, "-m", SD_MODEL, "--lora-model-dir", LORA_DIR,
        "-p", PROMPT_TEMPLATE.format(concept=concept),
        "-n", NEGATIVE,
        "--steps", str(steps), "--cfg-scale", "6",
        "-W", str(size), "-H", str(size),
        "--seed", str(seed), "-o", str(out_path),
    ]
    try:
        subprocess.run(cmd, timeout=300, capture_output=True, check=False)
    except Exception as e:
        print(f"  sd-cli Fehler: {e}", file=sys.stderr)
        return False
    return out_path.is_file() and out_path.stat().st_size > 1000


def main() -> None:
    ap = argparse.ArgumentParser(description="Piktogramme für Einträge erzeugen")
    ap.add_argument("--language", default="de")
    ap.add_argument("--limit", type=int, default=0,
                    help="max. Anzahl Bilder (0 = alle ohne Bild)")
    ap.add_argument("--size", type=int, default=384)
    ap.add_argument("--steps", type=int, default=16)
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="anderer Seed für Neuversuche (z. B. nach Voting)")
    args = ap.parse_args()

    lang_name = LANG_NAMES.get(args.language, f"{args.language}-sprachige")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Einträge ohne Bild einsammeln
    todo: list[dict] = []
    offset = 0
    while True:
        import urllib.parse
        page = api_get("/entries?" + urllib.parse.urlencode({
            "language": args.language, "noimage": "true",
            "limit": 500, "offset": offset,
        }))
        if not page:
            break
        todo += page
        offset += len(page)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} Einträge ohne Bild ({args.language})")

    done = failed = 0
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        concepts = translate_batch([e["word"] for e in chunk], lang_name)
        for e in chunk:
            concept = concepts.get(e["word"]) or e["word"]
            filename = f"{args.language}-{e['id']}.png"
            out = CACHE_DIR / filename
            if out.is_file() and out.stat().st_size > 1000:
                ok = True  # Cache-Treffer: nur hochladen
            else:
                seed = (e["id"] + args.seed_offset * 1_000_003) % 2**31
                ok = render(concept, out, args.size, args.steps, seed)
            if ok:
                try:
                    api_upload_image(e["id"], filename, out.read_bytes())
                    done += 1
                except Exception as e2:
                    print(f"  {e['word']!r}: Upload-Fehler: {e2}",
                          file=sys.stderr)
                    failed += 1
            else:
                print(f"  {e['word']!r} ({concept}): Render-Fehler",
                      file=sys.stderr)
                failed += 1
            time.sleep(args.delay)
        print(f"[{args.language}] {min(i + 50, len(todo))}/{len(todo)} "
              f"bearbeitet — {done} hochgeladen, {failed} Fehler")

    print(f"fertig: {done} Bilder hochgeladen, {failed} Fehler")


if __name__ == "__main__":
    main()
