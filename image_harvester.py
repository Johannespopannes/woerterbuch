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
import re
import shutil
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

# sd-server (stable-diffusion.cpp, A1111-kompatible API) mit geladenem
# Flux.1-schnell — kein Modell-Reload pro Bild (~17 s statt ~37 s)
SD_SERVER = os.environ.get("SD_SERVER", "http://127.0.0.1:8100")

PROMPT_TEMPLATE = (
    "flat vector pictogram of {concept}, minimal geometric icon, "
    "solid colors, centered, isolated on pure white background"
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


def api_store_image(filename: str, data: bytes) -> None:
    """Kandidaten-Entwurf speichern, OHNE entries.image zu ändern —
    damit die Voting-Seite Alternativen anbieten kann."""
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}/bilder/upload",
        data=json.dumps({
            "filename": filename,
            "data_b64": base64.b64encode(data).decode(),
        }).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except Exception as e:
        print(f"  Kandidaten-Upload {filename}: {e}", file=sys.stderr)


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
    """Ein Piktogramm über den sd-server rendern (Flux bleibt geladen).

    True bei Erfolg. Flux.1-schnell: cfg_scale 1, wenige Schritte,
    kein Negativ-Prompt nötig (destilliertes Modell).
    """
    body = {
        "prompt": PROMPT_TEMPLATE.format(concept=concept),
        "steps": steps,
        "width": size,
        "height": size,
        "cfg_scale": 1,
        "seed": seed,
    }
    req = urllib.request.Request(
        f"{SD_SERVER}/sdapi/v1/txt2img",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
        out_path.write_bytes(base64.b64decode(data["images"][0]))
    except Exception as e:
        print(f"  sd-server Fehler: {e}", file=sys.stderr)
        return False
    return out_path.is_file() and out_path.stat().st_size > 1000


GEMMA_URL = os.environ.get("GEMMA_URL", "http://127.0.0.1:8082/v1")

PICK_PROMPT = (
    'Die folgenden {n} Bilder (Bild 1 bis Bild {n}) sollen jeweils das Wort '
    '"{word}" als einfaches Piktogramm darstellen. Welches Bild macht "{word}" '
    'für einen Unbeteiligten am besten erkennbar? '
    'Antworte mit GENAU einer Zeile im Format:\nPICK: <Nummer>'
)


def gemma_pick(word: str, paths: list[Path]) -> int:
    """0-basierter Index des besten Kandidaten laut Gemma-Vision, -1 bei Fehler."""
    content = [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,"
                      + base64.b64encode(p.read_bytes()).decode()},
    } for p in paths]
    content.append({"type": "text", "text": PICK_PROMPT.format(
        n=len(paths), word=word)})
    body = {
        "model": "gemma-vision",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 800,  # Reasoning-Modell: Denkpfad braucht Budget
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        f"{GEMMA_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=300))
        out = r["choices"][0]["message"].get("content") or ""
    except Exception as e:
        print(f"  Gemma-Pick-Fehler: {e}", file=sys.stderr)
        return -1
    m = re.search(r"PICK:\s*(\d+)", out)
    if m and 1 <= int(m.group(1)) <= len(paths):
        return int(m.group(1)) - 1
    return -1


def main() -> None:
    ap = argparse.ArgumentParser(description="Piktogramme für Einträge erzeugen")
    ap.add_argument("--language", default="de")
    ap.add_argument("--limit", type=int, default=0,
                    help="max. Anzahl Bilder (0 = alle ohne Bild)")
    ap.add_argument("--size", type=int, default=384)
    ap.add_argument("--steps", type=int, default=4,
                    help="Flux.1-schnell ist destilliert: 4 Schritte reichen")
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="anderer Seed für Neuversuche (z. B. nach Voting)")
    ap.add_argument("--candidates", type=int, default=2,
                    help="Kandidaten pro Wort; Gemma-Vision wählt den besten "
                         "(1 = ein Rendervorgang ohne Auswahl)")
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
        # Pause-Datei: andere Jobs (z. B. fix_images) können die GPU für
        # sich reservieren, ohne diesen Langläufer abzuwürgen
        pause = CACHE_DIR.parent / "PAUSE"
        while pause.is_file():
            print("  pausiert (PAUSE-Datei) …", flush=True)
            time.sleep(20)
        chunk = todo[i:i + 50]
        concepts = translate_batch([e["word"] for e in chunk], lang_name)
        for e in chunk:
            try:
                concept = concepts.get(e["word"]) or e["word"]
                filename = f"{args.language}-{e['id']}.png"
                out = CACHE_DIR / filename
                cand_paths: list[Path] = []
                if out.is_file() and out.stat().st_size > 1000:
                    ok = True  # Cache-Treffer: nur hochladen
                elif args.candidates <= 1:
                    seed = (e["id"] + args.seed_offset * 1_000_003) % 2**31
                    ok = render(concept, out, args.size, args.steps, seed)
                else:
                    # Best-of-k: k Seeds rendern, Gemma-Vision wählt den Sieger
                    cand_files = []
                    for ci in range(args.candidates):
                        cand = CACHE_DIR / (
                            f"{args.language}-{e['id']}-cand{ci + 1}.png")
                        seed = (e["id"] + args.seed_offset * 1_000_003
                                + ci * 104_729) % 2**31
                        if (cand.is_file() and cand.stat().st_size > 1000) or \
                                render(concept, cand, args.size, args.steps,
                                       seed):
                            cand_files.append(cand)
                    cand_paths = cand_files
                    if not cand_files:
                        ok = False
                    else:
                        pick = gemma_pick(e["word"], cand_files) \
                            if len(cand_files) > 1 else 0
                        shutil.copyfile(
                            cand_files[pick] if pick >= 0 else cand_files[0],
                            out)
                        ok = True
                if ok:
                    try:
                        api_upload_image(e["id"], filename, out.read_bytes())
                        done += 1
                        # Kandidaten-Entwürfe ebenfalls hochladen, damit
                        # die Voting-Seite Alternativen zum Anklicken hat
                        for cand in cand_paths:
                            api_store_image(cand.name, cand.read_bytes())
                    except Exception as e2:
                        print(f"  {e['word']!r}: Upload-Fehler: {e2}",
                              file=sys.stderr)
                        failed += 1
                else:
                    print(f"  {e['word']!r} ({concept}): Render-Fehler",
                          file=sys.stderr)
                    failed += 1
            except Exception as e3:
                # Ein einzelnes Wort darf den Langläufer nie killen
                print(f"  {e['word']!r}: unerwarteter Fehler: {e3}",
                      file=sys.stderr)
                failed += 1
            time.sleep(args.delay)
        print(f"[{args.language}] {min(i + 50, len(todo))}/{len(todo)} "
              f"bearbeitet — {done} hochgeladen, {failed} Fehler")

    print(f"fertig: {done} Bilder hochgeladen, {failed} Fehler")
    # Fehler (z. B. 502er Uploads) ueber Nacht nicht aufgeben: Rueckgabe-
    # code != 0 -> die Nacht-Pipeline startet die Sprache erneut.
    if failed and not args.limit:
        sys.exit(2)


if __name__ == "__main__":
    main()
