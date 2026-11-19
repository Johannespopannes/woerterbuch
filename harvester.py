#!/usr/bin/env python3
"""Wörter-Harvester: fragt ein OpenAI-kompatibles Modell systematisch nach Wörtern.

Drei Strategien (--mode):
    free    Alphabet durchlaufen, freien Recall pro Buchstabe (mehrere Runden,
            hohe Temperatur -> unterschiedliche Wörter)
    suggest Bekannte Wörter zeigen, Modell darf Vorschläge zum Erweitern machen
    gap     Zwei Nachbarn aus der sortierten Liste nehmen und gezielt fragen,
            welche Wörter alphabetisch DAZWISCHEN liegen (füllt Lücken)

Dazu: eingebauter Startbestand (Seeds pro Buchstabe), siehe --seed.

Konfiguration per Umgebungsvariable:
    MODEL_BASE_URL   z. B. http://home-server:11434/v1  (Ollama)
                                http://home-server:8000/v1  (vLLM)
                                oder leer lassen für api.openai.com
    MODEL_API_KEY    API-Key (bei Ollama egal, aber muss gesetzt sein)
    MODEL_NAME       Modellname, z. B. kolibri-1, llama3.1
    DICT_API_URL     http://127.0.0.1:8000
    DICT_API_KEY     X-API-Key für Schreibzugriffe an die Wörterbuch-API

Aufruf-Beispiele:
    python3 harvester.py --seed --language de
    python3 harvester.py --mode suggest --language de --letters all --rounds 5
    python3 harvester.py --mode gap --language de --letters all
"""
import argparse
import json
import os
import random
import re
import string
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

MODEL_BASE_URL = os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "dummy")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")
DICT_API_URL = os.environ.get("DICT_API_URL", "http://127.0.0.1:8000")

LANG_NAMES = {
    "de": "deutsche",
    "en": "englische",
    "fr": "französische",
    "es": "spanische",
    "it": "italienische",
}

# Sprachen, in denen Akzente Diakritika auf dem Basisbuchstaben sind
# (é → e). Im Deutschen sind ä/ö/ü eigene Buchstaben — dort NICHT falten.
ACCENT_FOLD_LANGS = {"en", "fr", "es", "it"}


def _first_letter(word: str, language: str) -> str:
    """Anfangsbuchstabe für den Buchstaben-Filter, ggf. akzentgefaltet."""
    c = word[0].lower()
    if language in ACCENT_FOLD_LANGS:
        c = unicodedata.normalize("NFD", c)[0]
    return c

# ---------- Prompts ----------

PROMPT_FREE = (
    "Liste genau {n} {lang} Wörter (Grundformen, Gemeinsprache, keine "
    "Eigennamen, keine Abkürzungen), die mit dem Buchstaben \"{letter}\" "
    "beginnen. Antworte NUR mit einem JSON-Array von Strings, keine "
    "Erklärung, kein Markdown."
)

PROMPT_SUGGEST = (
    "Hier sind bekannte {lang} Wörter, die mit \"{letter}\" beginnen:\n"
    "{known}\n"
    "Nenne genau {n} weitere {lang} Wörter mit \"{letter}\", die NICHT in "
    "der Liste stehen (Grundformen, Gemeinsprache, keine Eigennamen). "
    "Antworte NUR mit einem JSON-Array von Strings."
)

PROMPT_GAP = (
    "Welche {lang} Wörter liegen im Wörterbuch-alphabetisch SORTIERT "
    "zwischen \"{a}\" und \"{b}\"? Nenne genau {n} Treffer (Grundformen, "
    "Gemeinsprache). Antworte NUR mit einem JSON-Array von Strings."
)

PROMPT_DEFINE = (
    "Definiere die folgenden {lang} Wörter jeweils in höchstens 15 Wörtern "
    "(Wortart + Kernbedeutung, keine Beispielsätze, keine Ausschmückung). "
    "Fremdwörter und Anglizismen: Herkunftssprache und das Originalwort in "
    "Klammern angeben, z. B. 'Substantiv (aus englisch to bail out …): …'. "
    "Antworte NUR mit einem JSON-Objekt, das jedes Wort auf seine Definition "
    "abbildet, keine Erklärung, kein Markdown.\n"
    "Wörter: {words}"
)

# ---------- Startbestand (Seeds) ----------

SEEDS = {
    "de": {
        "a": ["Apfel", "Arm", "Abend", "Auge"], "b": ["Berg", "Buch", "Baum", "Birne"],
        "c": ["Computer", "Club", "Chemie", "Café"], "d": ["Dorf", "Dach", "Donner", "Decke"],
        "e": ["Eltern", "Essen", "Elefant", "Ecke"], "f": ["Fenster", "Fisch", "Feld", "Frage"],
        "g": ["Garten", "Gras", "Gabel", "Glocke"], "h": ["Haus", "Hand", "Hafen", "Himmel"],
        "i": ["Insel", "Idee", "Instrument", "Igel"], "j": ["Jahr", "Jacke", "Joghurt", "Journal"],
        "k": ["Katze", "Kind", "Kirche", "Kuchen"], "l": ["Lampe", "Land", "Liebe", "Lehrer"],
        "m": ["Mutter", "Mond", "Mauer", "Musik"], "n": ["Nacht", "Name", "Natur", "Nase"],
        "o": ["Osten", "Ofen", "Obst", "Ort"], "p": ["Papier", "Pferd", "Politik", "Pflanze"],
        "q": ["Qualität", "Quelle", "Quittung", "Quark"], "r": ["Regen", "Radio", "Rose", "Recht"],
        "s": ["Sonne", "Stadt", "Sand", "Stuhl"], "t": ["Tisch", "Tag", "Topf", "Traum"],
        "u": ["Uhr", "Ufer", "Uniform", "Urlaub"], "v": ["Vater", "Volk", "Vulkan", "Vogel"],
        "w": ["Wasser", "Wald", "Woche", "Wind"], "x": ["Xylophon", "Xenon"],
        "y": ["Yacht", "Yoga", "Yucca"], "z": ["Zeit", "Zelt", "Zucker", "Zwiebel"],
    },
    "en": {
        "a": ["apple", "arm", "autumn", "anchor"], "b": ["book", "bread", "bridge", "beach"],
        "c": ["chair", "cloud", "castle", "candle"], "d": ["door", "dream", "dinner", "desert"],
        "e": ["earth", "eagle", "engine", "evening"], "f": ["fire", "forest", "friend", "flower"],
        "g": ["garden", "gold", "grape", "guitar"], "h": ["house", "horse", "harbor", "hammer"],
        "i": ["island", "idea", "iron", "insect"], "j": ["jungle", "jacket", "jewel", "journal"],
        "k": ["kitchen", "kite", "kettle", "kingdom"], "l": ["lamp", "ladder", "lemon", "library"],
        "m": ["moon", "mountain", "market", "mirror"], "n": ["night", "needle", "noodle", "nation"],
        "o": ["ocean", "olive", "orchard", "orange"], "p": ["paper", "palace", "pillow", "pepper"],
        "q": ["queen", "quiet", "quarrel", "quill"], "r": ["river", "rainbow", "rocket", "ribbon"],
        "s": ["stone", "shadow", "storm", "silver"], "t": ["table", "thunder", "tunnel", "timber"],
        "u": ["umbrella", "uncle", "uniform", "union"], "v": ["valley", "velvet", "village", "violin"],
        "w": ["window", "winter", "wheat", "wagon"], "x": ["xenon", "xylophone"],
        "y": ["yellow", "yarn", "yard", "yogurt"], "z": ["zebra", "zipper", "zenith", "zephyr"],
    },
}


# ---------- API-Hilfen ----------

def chat(prompt: str, temperature: float) -> str:
    """Ein Chat-Request gegen einen OpenAI-kompatiblen Endpunkt (nur stdlib)."""
    req = urllib.request.Request(
        f"{MODEL_BASE_URL}/chat/completions",
        data=json.dumps({
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": 2000,
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MODEL_API_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def parse_words(raw: str) -> list[str]:
    """JSON-Array aus der Modellantwort kratzen (tolerant gegen Markdown-Fences)."""
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
        if not m:
            return []
        try:
            items = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(items, list):
        return []
    return [str(w).strip() for w in items if isinstance(w, (str, int))]


def parse_definitions(raw: str) -> dict[str, str]:
    """JSON-Objekt {Wort: Definition} aus der Modellantwort kratzen."""
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k).strip(): str(v).strip()
            for k, v in obj.items()
            if str(k).strip() and str(v).strip()}


DICT_API_KEY = os.environ.get("DICT_API_KEY")  # für POST/DELETE an die API


def api_get(path: str):
    with urllib.request.urlopen(f"{DICT_API_URL}{path}", timeout=30) as resp:
        return json.loads(resp.read())


def api_post(path: str, payload: dict):
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def api_patch(path: str, payload: dict):
    headers = {"Content-Type": "application/json"}
    if DICT_API_KEY:
        headers["X-API-Key"] = DICT_API_KEY
    req = urllib.request.Request(
        f"{DICT_API_URL}{path}",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="PATCH",
    )
    last: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last = e
            time.sleep(2 * attempt)
    raise last


def letter_words(language: str, letter: str) -> list[str]:
    """Alle bekannten Wörter eines Buchstabens, sortiert (seitenweise von der API)."""
    out: list[str] = []
    offset = 0
    while True:
        page = api_get(
            "/entries?" + urllib.parse.urlencode({
                "language": language, "prefix": letter,
                "limit": 1000, "offset": offset,
            })
        )
        if not page:
            break
        out += [e["word"] for e in page]
        offset += len(page)
    return sorted(set(out), key=str.lower)


def insert(language: str, words: list[str]) -> int:
    if not words:
        return 0
    # API-Payload-Grenzen beachten (Wort max. 100 Zeichen)
    words = [w for w in words if len(w) <= 100]
    if not words:
        return 0
    try:
        result = api_post("/entries/batch", {
            "entries": [
                {"word": w, "language": language, "source": f"model:{MODEL_NAME}"}
                for w in words
            ]
        })
        return result["inserted"]
    except urllib.error.HTTPError as e:
        # Einzelne ungültige Wörter nicht den ganzen Batch kosten lassen
        print(f"Batch-Fehler {e.code} — versuche einzeln", file=sys.stderr)
        added = 0
        for w in words:
            try:
                api_post("/entries", {
                    "word": w, "language": language,
                    "source": f"model:{MODEL_NAME}",
                })
                added += 1
            except urllib.error.HTTPError as single:
                if single.code == 409:
                    continue  # Duplikat
                print(f"  übersprungen ({single.code}): {w!r}", file=sys.stderr)
        return added


# ---------- Strategien ----------

def harvest_free(language, letters, rounds, per_round, temperature, delay):
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    for letter in letters:
        known = set(letter_words(language, letter))
        before = len(known)
        for rnd in range(1, rounds + 1):
            try:
                cand = parse_words(chat(
                    PROMPT_FREE.format(n=per_round, lang=lang, letter=letter), temperature))
            except Exception as e:
                print(f"[{letter}] Runde {rnd}: Modell-Fehler: {e}", file=sys.stderr)
                time.sleep(delay * 3)
                continue
            new = [w for w in cand
                   if w and _first_letter(w, language) == letter
                   and w not in known]
            added = insert(language, new)
            known.update(new)
            print(f"[{letter}] Runde {rnd}: +{added} neu")
            time.sleep(delay)
        print(f"=> {letter}: {len(known) - before} neue Wörter ({len(known)} gesamt)")


def harvest_suggest(language, letters, max_rounds, per_round, temperature, delay):
    """Bekannte Wörter zeigen und erweitern lassen — bis nichts mehr kommt."""
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    for letter in letters:
        rnd = 0
        while rnd < max_rounds:
            rnd += 1
            known = letter_words(language, letter)
            sample = random.sample(known, min(10, len(known))) if known else []
            prompt = PROMPT_SUGGEST.format(
                lang=lang, letter=letter,
                known=", ".join(sample) if sample else "(noch keine bekannt)",
                n=per_round)
            try:
                cand = parse_words(chat(prompt, temperature))
            except Exception as e:
                print(f"[{letter}] Runde {rnd}: Modell-Fehler: {e}", file=sys.stderr)
                time.sleep(delay * 3)
                break
            known_set = set(known)
            new = [w for w in cand
                   if w and _first_letter(w, language) == letter
                   and w not in known_set]
            added = insert(language, new)
            print(f"[{letter}] Runde {rnd}: +{added} neu ({len(known)} vorher bekannt)")
            if added == 0:
                break
            time.sleep(delay)


def harvest_gap(language, letters, max_rounds, per_round, temperature, delay, pairs_per_round):
    """Gezielt Lücken schließen: Wörter zwischen zwei Nachbarn erfragen."""
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    for letter in letters:
        rnd = 0
        while rnd < max_rounds:
            rnd += 1
            words = letter_words(language, letter)
            if len(words) < 2:
                print(f"[{letter}] weniger als 2 bekannte Wörter — "
                      f"erst Seeds oder suggest nutzen")
                break
            idx = random.sample(range(len(words) - 1),
                                k=min(pairs_per_round, len(words) - 1))
            new_total = 0
            for i in sorted(idx):
                a, b = words[i], words[i + 1]
                prompt = PROMPT_GAP.format(lang=lang, a=a, b=b, n=per_round)
                try:
                    cand = parse_words(chat(prompt, temperature))
                except Exception as e:
                    print(f"[{letter}] Paar {a}|{b}: Modell-Fehler: {e}",
                          file=sys.stderr)
                    time.sleep(delay * 3)
                    continue
                gap = [w for w in cand
                       if w and a.lower() < w.lower() < b.lower()]
                new_total += insert(language, gap)
                time.sleep(delay)
            print(f"[{letter}] Runde {rnd}: +{new_total} neu "
                  f"({len(words)} vorher bekannt)")
            if new_total == 0:
                break


def _define_batch(batch: list[dict], lang: str, temperature: float,
                  depth: int = 0) -> int:
    """Definiert einen Batch in einem Modellaufruf.

    Schlägt der Aufruf oder das JSON-Parsen fehl, wird der Batch halbiert
    und erneut versucht (bis Tiefe 3) — große Blöcke verlieren so bei
    einem einzelnen Fehler nicht gleich alle Wörter.
    """
    words = [e["word"] for e in batch]
    try:
        defs = parse_definitions(chat(
            PROMPT_DEFINE.format(lang=lang, words=", ".join(words)),
            temperature))
    except Exception as e:
        if len(batch) > 5 and depth < 3:
            half = len(batch) // 2
            print(f"  Batch mit {len(batch)} Wörtern fehlgeschlagen ({e}) — "
                  f"teile in {half}+{len(batch) - half}", file=sys.stderr)
            return (_define_batch(batch[:half], lang, temperature, depth + 1)
                    + _define_batch(batch[half:], lang, temperature, depth + 1))
        print(f"  Batch-Fehler ({len(batch)} Wörter): {e}", file=sys.stderr)
        return 0
    added = 0
    for e in batch:
        d = defs.get(e["word"])
        if not d or len(d) > 1000:
            continue
        try:
            api_patch(f"/entries/{e['id']}", {"definition": d})
            added += 1
        except Exception as e2:
            print(f"  {e['word']!r}: PATCH-Fehler: {e2}", file=sys.stderr)
    return added


def harvest_define(language: str, batch_size: int, delay: float,
                   temperature: float = 0.3) -> None:
    """Definitionen für alle Einträge ohne definition nachrüsten.

    Holt die undefinierten Einträge einmalig als Snapshot (seitenweise) und
    definiert sie in Batches — ein Modellaufruf pro batch_size Wörter,
    ein PATCH pro Wort. Bereits definierte Einträge werden nie angefasst.
    """
    lang = LANG_NAMES.get(language, f"{language}-Sprachige")
    todo: list[dict] = []
    offset = 0
    while True:
        page = api_get(
            "/entries?" + urllib.parse.urlencode({
                "language": language, "undefined": "true",
                "limit": 1000, "offset": offset,
            })
        )
        if not page:
            break
        todo += page
        offset += len(page)
    print(f"{len(todo)} Einträge ohne Definition")
    total = 0
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        added = _define_batch(batch, lang, temperature)
        total += added
        print(f"Batch {i // batch_size + 1}: +{added} Definitionen "
              f"({total}/{len(todo)})")
        time.sleep(delay)
    print(f"define fertig: {total} von {len(todo)} definiert")


def seed(language: str) -> None:
    seeds = SEEDS.get(language)
    if not seeds:
        print(f"Keine Seeds für '{language}' eingebaut — bitte selbst per API eintragen.")
        return
    words = [w for letter_words_list in seeds.values() for w in letter_words_list]
    result = api_post("/entries/batch", {
        "entries": [
            {"word": w, "language": language, "source": "seed"} for w in words
        ]
    })
    print(f"Seeds {language}: {result}")


# ---------- CLI ----------

def parse_letters(spec: str) -> list[str]:
    if spec == "all":
        return list(string.ascii_lowercase) + ["ä", "ö", "ü", "ß"]
    if re.fullmatch(r"[a-z]-[a-z]", spec):
        return list(string.ascii_lowercase[
            string.ascii_lowercase.index(spec[0]):
            string.ascii_lowercase.index(spec[2]) + 1
        ])
    return [l.strip() for l in spec.split(",") if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Wörter aus einem LLM in die Wörterbuch-API harvesten")
    ap.add_argument("--mode", choices=["free", "suggest", "gap", "define"],
                    default="suggest")
    ap.add_argument("--language", default="de")
    ap.add_argument("--letters", default="a-z", help="'a-z', 'a-f', 'all', 'ä,ö,ü,ß'")
    ap.add_argument("--rounds", type=int, default=3, help="Runden pro Buchstabe (gap/suggest: Abbruch bei 0 neu)")
    ap.add_argument("--per-round", type=int, default=80,
                    help="Wörter pro Modellaufruf (define: Batchgröße)")
    ap.add_argument("--pairs", type=int, default=4, help="Paare pro gap-Runde")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--seed", action="store_true", help="Startbestand einspielen und beenden")
    args = ap.parse_args()

    if args.seed:
        seed(args.language)
        return

    if args.mode == "define":
        print(f"Modus: define | Modell: {MODEL_NAME} @ {MODEL_BASE_URL}")
        harvest_define(args.language, args.per_round, args.delay)
        print(f"\nFertig. DB-Stand: {api_get('/stats')}")
        return

    letters = parse_letters(args.letters)
    print(f"Modus: {args.mode} | Modell: {MODEL_NAME} @ {MODEL_BASE_URL}\n"
          f"Buchstaben: {''.join(letters)}")

    if args.mode == "free":
        harvest_free(args.language, letters, args.rounds, args.per_round,
                     args.temperature, args.delay)
    elif args.mode == "suggest":
        harvest_suggest(args.language, letters, args.rounds, args.per_round,
                        args.temperature, args.delay)
    else:
        harvest_gap(args.language, letters, args.rounds, args.per_round,
                    args.temperature, args.delay, args.pairs)

    print(f"\nFertig. DB-Stand: {api_get('/stats')}")


if __name__ == "__main__":
    main()
