"""Nachbesserungs-Pipeline für schlecht bewertete Wort-Bilder (v2).

Drei-Modelle-Kette je Eintrag (aus GET /raten/schlecht):
1. Aktuelles Bild vom Server laden, Gemma 4 (Vision, :8082) kritisiert,
   was zu sehen ist und warum man das Wort nicht erkennt
2. Mistral Small 4 (:8081) schlägt auf Basis von Wort, Definition und
   Kritik 3 verschiedene konkrete englische Bildmotive vor
3. Jedes Motiv wird mit eigenem Seed gerendert (sd-cli + Icon-LoRA)
4. Gemma bewertet jeden Kandidaten 0-10 auf Erkennbarkeit
5. Bester Kandidat wird hochgeladen — neuer Dateiname mit Versionszähler,
   damit Browser-Caches nicht das alte Bild zeigen

Beispiel:
    python fix_images.py --min-votes 2 --candidates 3
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request

from image_harvester import CACHE_DIR, api_upload_image, render

DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")
DICT_API_KEY = os.environ.get("DICT_API_KEY", "")
GEMMA_URL = os.environ.get("GEMMA_URL", "http://127.0.0.1:8082/v1")
MISTRAL_URL = os.environ.get("MISTRAL_URL", "http://127.0.0.1:8081/v1")

CRITIQUE_PROMPT = (
    'Dieses Bild soll das Wort "{word}" als einfaches Piktogramm darstellen '
    '(Bedeutung: "{definition}"). Menschen haben es als NICHT verständlich '
    'bewertet.\n'
    '1) Was ist wirklich zu sehen? 2) Warum erkennt man "{word}" nicht?\n'
    'Antworte in höchstens 3 kurzen Sätzen.'
)

PROPOSAL_PROMPT = (
    'Das Wort "{word}" (Bedeutung: "{definition}") soll als flaches '
    'Piktogramm / Icon dargestellt werden.\n'
    'Kritik am bisherigen Bild: {critique}\n'
    'Schlage 3 VERSCHIEDENE konkrete englische Bildmotive vor. Regeln: je '
    'höchstens 12 Wörter, nur das sichtbare Motiv beschreiben (keine '
    'Stilangaben), jedes Motiv muss das Wort für sich allein erkennbar '
    'machen, abstrakte Wörter auf ein alltagstaugliches Symbol bringen.\n'
    'Antworte mit GENAU 3 Zeilen im Format:\n'
    '1. <Motiv>\n2. <Motiv>\n3. <Motiv>'
)

SCORE_PROMPT = (
    'Dieses Piktogramm soll das Wort "{word}" darstellen '
    '(Bedeutung: "{definition}"). Wie gut erkennt ein Unbeteiligter das '
    'Wort allein am Bild? Antworte mit GENAU einer Zeile im Format:\n'
    'SCORE: <ganze Zahl 0-10>'
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


def chat(url: str, model: str, content, max_tokens: int,
         temperature: float = 0.4) -> str | None:
    """Ein Chat-Completion-Aufruf gegen eine OpenAI-kompatible API."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        f"{url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=300))
    except Exception as e:
        print(f"  LLM-Fehler ({model}): {e}", file=sys.stderr)
        return None
    return r["choices"][0]["message"].get("content") or ""


def gemma_critique(word: str, definition: str, png: bytes) -> str:
    img = base64.b64encode(png).decode()
    out = chat(GEMMA_URL, "gemma-vision", [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + img}},
        {"type": "text", "text": CRITIQUE_PROMPT.format(
            word=word, definition=(definition or "").strip()[:200])},
    ], max_tokens=1000, temperature=0.3)  # Reasoning-Modell: Budget nötig
    return (out or "").strip() or "keine Kritik verfügbar"


def mistral_proposals(word: str, definition: str, critique: str,
                      n: int) -> list[str]:
    out = chat(MISTRAL_URL, "mistral-small-4", PROPOSAL_PROMPT.format(
        word=word, definition=(definition or "").strip()[:200],
        critique=critique[:400]), max_tokens=400, temperature=0.8)
    if not out:
        return []
    ideas = []
    for line in out.splitlines():
        m = re.match(r"^\s*\d+[.)]\s*(.+)$", line.strip())
        if m:
            idea = m.group(1).strip().strip('"')
            if 3 <= len(idea) <= 120:
                ideas.append(idea)
    return ideas[:n]


def gemma_score(word: str, definition: str, png_path) -> int:
    img = base64.b64encode(png_path.read_bytes()).decode()
    out = chat(GEMMA_URL, "gemma-vision", [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + img}},
        {"type": "text", "text": SCORE_PROMPT.format(
            word=word, definition=(definition or "").strip()[:200])},
    ], max_tokens=800, temperature=0.1)
    if not out:
        return -1
    m = re.search(r"SCORE:\s*(\d+)", out)
    if not m:
        # Reasoning-Modell: Score steht manchmal erst nach dem Denkblock
        m = re.search(r"(\d+)\s*(?:/\s*10)?\s*$", out.strip())
    return min(int(m.group(1)), 10) if m else -1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-votes", type=int, default=2)
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--size", type=int, default=384)
    ap.add_argument("--steps", type=int, default=4,
                    help="Flux.1-schnell ist destilliert: 4 Schritte reichen")
    args = ap.parse_args()

    entries = api_get(f"/raten/schlecht?min_votes={args.min_votes}")
    print(f"{len(entries)} schlecht bewertete Bilder")

    # GPU während des ganzen Laufs reservieren: der Harvester legt bei
    # vorhandener PAUSE-Datei eine Ruhepause ein statt zu konkurrieren
    pause = CACHE_DIR.parent / "PAUSE"
    pause.touch()
    jobs = []  # dicts: entry, critique, ideas, version, candidates[]
    try:
        # Phase 1: Bilder laden + Gemma-Kritik (nur Gemma aktiv)
        print("Phase 1: Gemma-Kritik …")
        for e in entries:
            try:
                png = download_image(e["image"])
            except Exception as ex:
                print(f"  {e['word']}: Bild nicht ladbar: {ex}",
                      file=sys.stderr)
                continue
            critique = gemma_critique(e["word"], e["definition"] or "", png)
            print(f"  {e['word']}: {critique[:100]}")
            jobs.append({"entry": e, "critique": critique})

        # Phase 2: Mistral-Motive (nur Mistral aktiv)
        print("Phase 2: Mistral-Motive …")
        for job in jobs:
            e = job["entry"]
            ideas = mistral_proposals(e["word"], e["definition"] or "",
                                      job["critique"], args.candidates)
            if not ideas:
                print(f"  {e['word']}: keine Vorschläge", file=sys.stderr)
            job["ideas"] = ideas

        jobs = [j for j in jobs if j["ideas"]]

        # Phase 3: alle Kandidaten am Stück rendern (nur sd-cli aktiv)
        print("Phase 3: Rendern …")
        for job in jobs:
            e = job["entry"]
            version = sum(1 for p in CACHE_DIR.glob(
                f"{e['language']}-{e['id']}-v*.png")) + 2
            job["version"] = version
            job["candidates"] = []
            for i, idea in enumerate(job["ideas"]):
                cand = CACHE_DIR / (
                    f"{e['language']}-{e['id']}-v{version}-cand{i + 1}.png")
                seed = (e["id"] * 7919 + version * 31 + i) % 2**31
                if render(idea, cand, args.size, args.steps, seed):
                    job["candidates"].append((idea, cand))
                else:
                    print(f"  {e['word']}: Kandidat {i + 1} Render-Fehler",
                          file=sys.stderr)

        # Phase 4: Gemma bewertet alle Kandidaten (nur Gemma aktiv)
        print("Phase 4: Bewertung …")
        for job in jobs:
            e = job["entry"]
            best = None  # (score, idea, path)
            for idea, cand in job["candidates"]:
                score = gemma_score(e["word"], e["definition"] or "", cand)
                print(f"  {e['word']}: Score {score} — {idea[:70]}")
                if best is None or score > best[0]:
                    best = (score, idea, cand)
            job["best"] = best

        # Phase 5: Sieger hochladen
        print("Phase 5: Upload …")
        fixed = failed = 0
        for job in jobs:
            e = job["entry"]
            if not job["best"]:
                failed += 1
                continue
            score, idea, cand = job["best"]
            filename = f"{e['language']}-{e['id']}-v{job['version']}.png"
            try:
                api_upload_image(e["id"], filename, cand.read_bytes())
                print(f"  {e['word']}: {filename} (Score {score})")
                fixed += 1
            except Exception as ex:
                print(f"  {e['word']}: Upload-Fehler: {ex}", file=sys.stderr)
                failed += 1
        print(f"fertig: {fixed} Bilder ersetzt, {failed} Fehler")
    finally:
        pause.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
