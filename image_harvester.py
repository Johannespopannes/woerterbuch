"""Bild-Pipeline: Piktogramme für Wörterbuch-Einträge.

Ablauf je Sprache:
1. Einträge ohne Bild holen (API-Filter noimage=true)
2. Mistral übersetzt Wörter portionsweise in kurze englische Bildmotive
   (SD 1.5 kann Deutsch kaum; ein LLM-Batch pro 50 Wörter)
3. sd-server (stable-diffusion.cpp, Vulkan) rendert das Piktogramm
4. Upload per POST /entries/{id}/image (Base64), lokaler Cache bleibt

Die Renders laufen NICHT mehr direkt gegen den sd-server, sondern als
Jobs in der gemeinsamen GPU-Queue (C:/Repos/_scratch/agent-tasks) — ein
FIFO für alle Agenten (woerterbuch + icons.tassekaffee.com), damit nie
zwei Prozesse gleichzeitig die GPU voll auslasten.

Beispiel:
    python image_harvester.py --language de --limit 50
    python image_harvester.py --direct --language de   (Queue umgehen)
"""
import argparse
import base64
import json
import os
import re
import shutil
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

# sd-server (stable-diffusion.cpp, A1111-kompatible API) mit geladenem
# Flux.1-schnell — kein Modell-Reload pro Bild (~17 s statt ~37 s)
SD_SERVER = os.environ.get("SD_SERVER", "http://127.0.0.1:8100")

# Cloud-Render über ModelHub (Wan auf dem Pi): keine lokale GPU nötig,
# ~11 s pro Bild. Aktiv via WB_RENDER_CLOUD=1.
CLOUD_IMAGE_URL = os.environ.get(
    "WB_CLOUD_IMAGE_URL",
    "https://modelhub.tassekaffee.com/v1/images/generations")
# Leer = ModelHub-Default (Wan). Alternativen siehe /v1/image/models.
CLOUD_IMAGE_MODEL = os.environ.get("WB_CLOUD_IMAGE_MODEL", "")
MODELHUB_URL = os.environ.get(
    "MODEL_BASE_URL", "https://modelhub.tassekaffee.com/v1")

PROMPT_TEMPLATE = (
    "flat vector pictogram of {concept}, minimal geometric icon, "
    "solid colors, centered, isolated on pure white background"
)
NEGATIVE = "photo, realistic, text, watermark, blurry, complex background"

TRANSLATE_PROMPT = (
    "Gib für jedes der folgenden {lang} Wörter ein kurzes, konkretes "
    "englisches Bildmotiv für ein Piktogramm an (AUSSCHLIESSLICH "
    "englisch, 1-3 englische Wörter, "
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


def _englisch_genug(motiv: str) -> bool:
    """Flux versteht fast nur Englisch — Motive ohne lateinische
    Buchstaben dominieren wuerden schlecht rendern (z. B. deutsche
    Woerter, die Mistral trotz Anweisung liefert)."""
    if not motiv:
        return False
    letters = [c for c in motiv if c.isalpha()]
    if not letters:
        return False
    latein = sum(1 for c in letters if c.isascii())
    return latein / len(letters) >= 0.95


def translate_batch(words: list[str], lang: str) -> dict[str, str]:
    """Wörter -> englische Bildmotive (ein LLM-Aufruf pro Liste).

    Motive, die nicht englisch_genug sind, werden verworfen — lieber
    fehlen (und werden einzeln nachgeholt) als auf Deutsch rendern.
    """
    try:
        out = parse_definitions(chat(
            TRANSLATE_PROMPT.format(lang=lang, words=", ".join(words)), 0.2))
        ok = {}
        for w in words:
            m = str(out.get(w, "")).strip()
            if _englisch_genug(m):
                ok[w] = m
            elif m:
                print(f"  {w!r}: Motiv nicht englisch ({m!r}) — verworfen",
                      file=sys.stderr)
        return ok
    except Exception as e:
        print(f"  Übersetzungs-Batch fehlgeschlagen: {e}", file=sys.stderr)
        return {}


def translate_one(word: str, lang: str) -> str | None:
    """Einzelwort-Nachholung, wenn der Batch es nicht geliefert hat.
    Versuch 1: JSON-Stil. Versuch 2: bloße englische Phrase."""
    try:
        out = parse_definitions(chat(
            TRANSLATE_PROMPT.format(lang=lang, words=word), 0.2))
        m = str(out.get(word, "")).strip()
        if _englisch_genug(m):
            return m
    except Exception as e:
        print(f"  {word!r}: Einzel-Übersetzung fehlgeschlagen: {e}",
              file=sys.stderr)
    try:
        raw = chat(
            "Translate the following word into a short, concrete English "
            "phrase for a pictogram icon (1-3 English words, visible "
            "object only, no style words). Reply with ONLY the English "
            f"phrase.\nWord: {word}", 0.2)
        m = raw.strip().strip('"').split("\n")[0][:80]
        if _englisch_genug(m):
            return m
    except Exception as e:
        print(f"  {word!r}: Einzel-Übersetzung fehlgeschlagen: {e}",
              file=sys.stderr)
    return None


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


def render_cloud(concept: str, out_path: Path, size: int, steps: int,
                 seed: int) -> bool:
    """Piktogramm über ModelHub-Cloud rendern (Flux-Lane, lokal auf Schleppi).

    OpenAI-kompatibles /v1/images/generations; kein API-Key nötig.
    Die Lane startet on-demand — erster Request nach Leerlauf zahlt den
    Kaltstart (~10 min Modellladung), danach ~17 s pro Bild. Läuft auf
    Schleppi parallel ein anderes Supervisor-Modell (z. B. Kolibri),
    antwortet die Lane 429 modelhub_busy: dann bis zu 8x mit Retry-After
    warten (Ein-Modell-Regel des Supervisors).
    """
    prompt = (PROMPT_TEMPLATE.format(concept=concept)
              + ", no text, no letters, no watermark, not photorealistic")
    body = {"prompt": prompt, "size": "1024x1024"}
    if CLOUD_IMAGE_MODEL:
        body["model"] = CLOUD_IMAGE_MODEL
    for versuch in range(1, 9):
        req = urllib.request.Request(
            CLOUD_IMAGE_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=660) as resp:
                data = json.loads(resp.read())
            out_path.write_bytes(
                base64.b64decode(data["data"][0]["b64_json"]))
            return out_path.is_file() and out_path.stat().st_size > 1000
        except urllib.error.HTTPError as e:
            code = e.code
            retry_after = 40
            try:
                info = json.loads(e.read())
                retry_after = int(
                    info.get("error", {}).get("retry_after", retry_after))
            except Exception:
                pass
            if code in (429, 503, 408) and versuch < 8:
                print(f"  cloud-render {code}, Versuch {versuch}/8, "
                      f"warte {retry_after + 5}s ...", file=sys.stderr)
                time.sleep(retry_after + 5)
                continue
            print(f"  cloud-render Fehler: HTTP {code}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"  cloud-render Fehler: {e}", file=sys.stderr)
            return False
    print("  cloud-render Fehler: nach 8 Versuchen noch blockiert",
          file=sys.stderr)
    return False


# ---------- Gemeinsame GPU-Queue (icons.tassekaffee.com / pipeline.gpuq) ----------
# Der Icons-Kimi stellt die dateibasierte Queue (pending/claimed/done/
# failed); Worker laufen auf dem Pi und orchestrieren GPU-Jobs ueber das
# Gateway (RAM-Guard GPUQ_MIN_FREE_GB, Prioritaeten high/medium/low).
# Wir reihen pro Bild einen Job ein ("image_harvester.py --render-one"),
# der auf dem Worker uebers Gateway rendert (Env muss im cmd stehen —
# gpuq-Jobs tragen kein eigenes env-Feld). Aktiv nur mit WB_RENDER_QUEUE=1;
# sonst Direkt-Cloud wie bisher. Einreihen schlaegt fehl → Direkt-Cloud.
_ICONS_REPO = os.environ.get(
    "WB_ICONS_REPO", "/home/johannes/icons.tassekaffee.com")
_ICONS_VENV_PY = os.environ.get(
    "WB_ICONS_VENV_PY", f"{_ICONS_REPO}/.venv-factory/bin/python")
if os.name == "nt":
    _QUEUE_DIR = Path(os.environ.get(
        "WB_GPUQ_DIR", r"C:\Repos\_scratch\agent-tasks"))
else:
    _QUEUE_DIR = Path(os.environ.get(
        "WB_GPUQ_DIR", "/mnt/disk2/icons-factory/queue"))


def _gpuq_submit(cmd: list, priority: str) -> str | None:
    """Job in die icons-Queue einreihen; liefert job_id oder None."""
    try:
        p = subprocess.run(
            [_ICONS_VENV_PY, "-m", "pipeline.gpuq", "submit",
             "--kind", "gpu", "--priority", priority, "--", *cmd],
            cwd=_ICONS_REPO, capture_output=True, text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"eingereiht \[[^\]]+\]:\s+(\S+)", out)
        if p.returncode == 0 and m:
            return m.group(1)
        print(f"gpuq-submit fehlgeschlagen rc={p.returncode}: {out[:300]}",
              file=sys.stderr)
    except Exception as e:
        print(f"gpuq-submit fehlgeschlagen ({e})", file=sys.stderr)
    return None


def _gpuq_wait(job_id: str, timeout_s: int = 2700) -> bool:
    """Auf done/failed warten (Kaltstart der Lane braucht es bei ~10 min)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if (_QUEUE_DIR / "done" / f"{job_id}.json").is_file():
            return True
        f = _QUEUE_DIR / "failed" / f"{job_id}.json"
        if f.is_file():
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                print(f"Queue-Job FEHLGESCHLAGEN (exit {rec.get('exit')}): "
                      f"{f}", file=sys.stderr)
            except Exception:
                pass
            return False
        time.sleep(10)
    print(f"Queue-Job {job_id}: Timeout nach {timeout_s}s", file=sys.stderr)
    return False


def render_queued(concept: str, out_path: Path, size: int, steps: int,
                  seed: int, direct: bool = False) -> bool:
    """Rendern über die gemeinsame GPU-Queue (Standard) oder direkt.

    Der Job läuft als 'image_harvester.py --render-one ...' im Worker-
    Prozess und schreibt dieselbe Cache-Datei; wir warten auf done und
    prüfen dann nur noch das Ergebnis.
    """
    if out_path.is_file() and out_path.stat().st_size > 1000:
        return True  # Cache-Treffer
    # Cloud-Modus: Bilder ueber das ModelHub-Gateway (Flux-Lane auf
    # Schleppi). Mit WB_RENDER_QUEUE=1 wird jedes Bild als Job in die
    # icons-GPU-Queue eingereiht (Serialisierung + RAM-Guard); sonst
    # direkt rendern.
    if os.environ.get("WB_RENDER_CLOUD") == "1":
        if os.environ.get("WB_RENDER_QUEUE") != "1":
            return render_cloud(concept, out_path, size, steps, seed)
        job_env = [
            "WB_RENDER_CLOUD=1",
            f"WB_CLOUD_IMAGE_MODEL={CLOUD_IMAGE_MODEL}",
            f"MODEL_BASE_URL={MODELHUB_URL}",
            f"MODEL_API_KEY={os.environ.get('MODEL_API_KEY', 'dummy')}",
        ]
        cmd = ["/usr/bin/env", *job_env, sys.executable,
               str(Path(__file__).resolve()),
               "--render-one", concept, str(out_path), str(size),
               str(steps), str(seed)]
        job_id = _gpuq_submit(cmd, os.environ.get("WB_QUEUE_PRIO", "medium"))
        if not job_id:
            return render_cloud(concept, out_path, size, steps, seed)
        # Stau-Abbau übers Wochenende: ein Bild darf bis zu 3 h in der
        # Queue warten, bevor es verfällt (dann Direkt-Cloud-Fallback im
        # nächsten Zyklus statt sofort).
        ok = _gpuq_wait(job_id, timeout_s=10_800)
        return ok and out_path.is_file() and out_path.stat().st_size > 1000
    # Läuft der Harvester selbst ALS Queue-Job (gpu), hält der Worker
    # bereits das GPU-Lock — innere Render-Jobs doppelt einzureihen
    # würde sich gegenseitig aushungern (Deadlock bis Timeout).
    if os.environ.get("GPUQ_MANAGED") == "1":
        return render(concept, out_path, size, steps, seed)
    if direct:
        return render(concept, out_path, size, steps, seed)


GEMMA_URL = os.environ.get("GEMMA_URL", "http://127.0.0.1:8082/v1")


# ---------- dHash-Dubletten-Check (aus icons.tassekaffee.com uebernommen) ----------
# Flux mit leicht variierten Seeds liefert manchmal fast identische Kandidaten.
# Solche Dubletten fluten die Alternativen-Reihe — Hamming-Distanz auf
# Difference-Hashes filtert sie, bevor sie hochgeladen werden.

def dhash(path: Path, size: int = 8) -> int | None:
    """Difference-Hash (dHash): 64-Bit-Fingerabdruck, robust gegen
    geringfuegige Groessen-/Farbabweichungen."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("L").resize((size + 1, size), Image.LANCZOS)
            px = list(im.getdata())
        bits = 0
        for row in range(size):
            base = row * (size + 1)
            for col in range(size):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1]
                                      else 0)
        return bits
    except Exception:
        return None


def dedupe_candidates(paths: list[Path], max_dist: int = 6) -> list[Path]:
    """Kandidaten filtern, die einem bereits behaltenen zu aehnlich sind."""
    keep: list[Path] = []
    hashes: list[int] = []
    for p in paths:
        h = dhash(p)
        if h is None or any(bin(h ^ k).count("1") <= max_dist
                            for k in hashes):
            print(f"  Dublette verworfen: {p.name}", flush=True)
            continue
        hashes.append(h)
        keep.append(p)
    return keep

PICK_PROMPT = (
    'Die folgenden {n} Bilder (Bild 1 bis Bild {n}) sollen jeweils das Wort '
    '"{word}" als einfaches Piktogramm darstellen. Welches Bild macht "{word}" '
    'für einen Unbeteiligten am besten erkennbar? '
    'Antworte mit GENAU einer Zeile im Format:\nPICK: <Nummer>'
)


def gemma_pick(word: str, paths: list[Path]) -> int:
    """0-basierter Index des besten Kandidaten laut Vision-LLM, -1 bei Fehler.

    WB_PICK_MODEL gesetzt (z. B. qwen3.8-flash) → Pick über ModelHub,
    sonst lokales Gemma-Vision (GEMMA_URL).
    """
    pick_model = os.environ.get("WB_PICK_MODEL", "")
    content = [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,"
                      + base64.b64encode(p.read_bytes()).decode()},
    } for p in paths]
    content.append({"type": "text", "text": PICK_PROMPT.format(
        n=len(paths), word=word)})
    body = {
        "model": pick_model or "gemma-vision",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 800,  # Reasoning-Modell: Denkpfad braucht Budget
        "temperature": 0.1,
    }
    url = (f"{MODELHUB_URL}/chat/completions" if pick_model
           else f"{GEMMA_URL}/chat/completions")
    req = urllib.request.Request(
        url,
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
    ap.add_argument("--direct", action="store_true",
                    help="GPU-Queue umgehen und direkt gegen den sd-server "
                         "rendern (nötig, wenn der Queue-Worker ausfällt)")
    ap.add_argument("--render-one", nargs=5,
                    metavar=("CONCEPT", "OUT", "SIZE", "STEPS", "SEED"),
                    help=argparse.SUPPRESS)  # intern: wird als Queue-Job aufgerufen
    args = ap.parse_args()

    # Queue-Worker-Modus: genau ein Rendervorgang, Ergebnis = Exit-Code.
    # Cloud-Modus rendert uebers Gateway (Job kommt mit WB_RENDER_CLOUD=1
    # aus der Queue eingereiht), sonst lokaler sd-server.
    if args.render_one:
        concept, out, size, steps, seed = args.render_one
        if os.environ.get("WB_RENDER_CLOUD") == "1":
            ok = render_cloud(concept, Path(out), int(size), int(steps),
                              int(seed))
        else:
            ok = render(concept, Path(out), int(size), int(steps),
                        int(seed))
        sys.exit(0 if ok else 1)

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
                concept = concepts.get(e["word"])
                if not concept:
                    # Kein deutsches Motiv an Flux — lieber einzeln
                    # nachholen und in diesem Durchlauf auslassen.
                    concept = translate_one(e["word"], lang_name)
                    if not concept:
                        print(f"  {e['word']!r}: kein englisches Motiv — "
                              f"übersprungen (kommt im nächsten Lauf dran)",
                              file=sys.stderr)
                        failed += 1
                        time.sleep(args.delay)
                        continue
                filename = f"{args.language}-{e['id']}.png"
                out = CACHE_DIR / filename
                cand_paths: list[Path] = []
                if out.is_file() and out.stat().st_size > 1000:
                    ok = True  # Cache-Treffer: nur hochladen
                elif args.candidates <= 1:
                    seed = (e["id"] + args.seed_offset * 1_000_003) % 2**31
                    ok = render_queued(concept, out, args.size, args.steps,
                                       seed, direct=args.direct)
                else:
                    # Best-of-k: k Seeds rendern, Gemma-Vision wählt den Sieger
                    cand_files = []
                    for ci in range(args.candidates):
                        cand = CACHE_DIR / (
                            f"{args.language}-{e['id']}-cand{ci + 1}.png")
                        seed = (e["id"] + args.seed_offset * 1_000_003
                                + ci * 104_729) % 2**31
                        if (cand.is_file() and cand.stat().st_size > 1000) or \
                                render_queued(concept, cand, args.size,
                                              args.steps, seed,
                                              direct=args.direct):
                            cand_files.append(cand)
                    cand_paths = dedupe_candidates(cand_files)
                    if not cand_paths:
                        ok = False
                    else:
                        pick = gemma_pick(e["word"], cand_paths) \
                            if len(cand_paths) > 1 else 0
                        shutil.copyfile(
                            cand_paths[pick] if pick >= 0 else cand_paths[0],
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
