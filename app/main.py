"""Wörterbuch-API — Fachmodell v2 (docs/fachmodell.md).

Start:  uvicorn app.main:app --reload
Doku:   http://127.0.0.1:8000/docs  (Swagger UI)

Datenmodell (drei Identitäten):
  Wortform  = Schreibweise (word_forms, keine Grammatik/Semantik)
  Lexem     = grammatische Identität (lexemes, eigene ID)
  Bedeutung = Definition/Beispiele/Bilder/Belege (meanings)
  Lesart    = (form, lexeme) in readings; Audio je (Lesart, Sprecher)

Kompatibilität: Die öffentlichen Pfade der Pipeline (entries, senses,
formen, grammatik, lookup, raten, anhoeren, ...) behalten Pfad und
Payload-Form; dahinter liegt das neue Modell. Da die Migration ID-erhaltend
ist (word_forms.id = lexemes.id = alte entries.id, meanings.id = alte
senses.id), sind Fremdschlüssel stabil. Neue, generische Frontends nutzen
das Bundle-Endpunkt-Paar /wortdaten (Wortseite) — die Darstellung baut
sich vollständig aus den vorhandenen Daten.

Schutz vor Missbrauch:
  - Schreibzugriffe (POST/DELETE) brauchen den Header X-API-Key,
    sofern die Umgebungsvariable API_KEY gesetzt ist. Ist sie nicht
    gesetzt, wird beim Start ein zufälliger Key generiert und einmalig
    geloggt — die API ist dann nie unbeabsichtigt offen.
  - Rate-Limiting pro IP (in-Memory-Middleware): lesen 240/Min, schreiben
    5/Min; bekanntgültiger Key: 600/Min (für den Harvester).
    Grenzen per Umgebungsvariable anpassbar: RATE_READ_PER_MIN,
    RATE_WRITE_PER_MIN, RATE_WRITE_KEYED_PER_MIN, RATE_VOTE_PER_MIN.
    Achtung: Zähler pro Prozess — bei mehreren Workern nicht global.
  - Payload-Grenzen: Wort max. 100 Zeichen, Batch max. 500 Einträge.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import (Depends, FastAPI, Header, HTTPException, Query, Request,
                     Response)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .db import get_conn, now_str, row_to_dict

API_KEY = os.environ.get("API_KEY")
RATE_READ = int(os.environ.get("RATE_READ_PER_MIN", "240"))
RATE_WRITE = int(os.environ.get("RATE_WRITE_PER_MIN", "5"))
RATE_WRITE_KEYED = int(os.environ.get("RATE_WRITE_KEYED_PER_MIN", "600"))
RATE_VOTE = int(os.environ.get("RATE_VOTE_PER_MIN", "60"))
WINDOW = 60  # Sekunden

IMAGES_DIR = Path(os.environ.get(
    "IMAGES_DIR", Path(__file__).resolve().parent.parent / "images"))
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}\.(png|webp)$")

# Piper-Sprachausgabe (Wörter anhören); auf dem Pi installiert unter
# /home/johannes/piper (Binary piper/piper, Stimmen voices/*.onnx).
# Pro Sprache mehrere Sprecher — die erste Stimme ist der Default.
# length_scale: Piper-Tempo (< 1.0 schneller, > 1.0 langsamer).
PIPER_DIR = Path(os.environ.get("PIPER_DIR", "/home/johannes/piper"))
PIPER_VOICES: dict[str, dict[str, dict]] = {
    "de": {
        # thorsten HIGH statt medium — deutlich natürlicher. karlsson (low)
        # und eva (x_low) ersetzt: x_low/low klingen roboterhaft/abgehackt
        # (Beschwerde 23.11.). mls als dritte, ruhige Stimme.
        "thorsten": {"model": "de_DE-thorsten-high.onnx",
                     "label": "Thorsten", "length_scale": 1.0},
        "thorsten_emotional": {
                    "model": "de_DE-thorsten_emotional-medium.onnx",
                    "label": "Thorsten (emotional)", "length_scale": 1.0},
        "mls": {"model": "de_DE-mls-medium.onnx",
                "label": "MLS", "length_scale": 1.0},
    },
    "en": {
        "alan": {"model": "en_GB-alan-medium.onnx",
                 "label": "Alan", "length_scale": 1.0},
        "lessac": {"model": "en_US-lessac-medium.onnx",
                   "label": "Lessac", "length_scale": 1.0},
    },
    "es": {
        "davefx": {"model": "es_ES-davefx-medium.onnx",
                   "label": "Davefx", "length_scale": 1.0},
        "sharvard": {"model": "es_ES-sharvard-medium.onnx",
                     "label": "Sharvard", "length_scale": 1.0},
    },
    "fr": {
        "siwis": {"model": "fr_FR-siwis-medium.onnx",
                  "label": "Siwis", "length_scale": 1.0},
        "upmc": {"model": "fr_FR-upmc-medium.onnx",
                 "label": "UPMC", "length_scale": 1.0},
    },
    "it": {
        "paola": {"model": "it_IT-paola-medium.onnx",
                  "label": "Paola", "length_scale": 1.0},
        "riccardo": {"model": "it_IT-riccardo-x_low.onnx",
                     "label": "Riccardo", "length_scale": 1.0},
    },
    "ar": {
        "kareem": {"model": "ar_JO-kareem-medium.onnx",
                   "label": "Kareem", "length_scale": 1.0},
    },
    # Tunesisch: Piper hat keine tn-Stimme — Texte ja, Audio nein (404).
    "tn": {},
    "ru": {
        "dmitri": {"model": "ru_RU-dmitri-medium.onnx",
                   "label": "Dmitri", "length_scale": 1.0},
        "irina": {"model": "ru_RU-irina-medium.onnx",
                  "label": "Irina", "length_scale": 1.0},
    },
    "zh": {
        "huayan": {"model": "zh_CN-huayan-medium.onnx",
                   "label": "Huayan", "length_scale": 1.0},
        "xiao_ya": {"model": "zh_CN-xiao_ya-medium.onnx",
                    "label": "Xiao Ya", "length_scale": 1.0},
    },
    "ja": {
        "hi_fi_captain": {"model": "ja_JP-hi_fi_captain-medium.onnx",
                          "label": "Hi-Fi Captain", "length_scale": 1.0},
    },
    "hi": {
        "pratham": {"model": "hi_IN-pratham-medium.onnx",
                    "label": "Pratham", "length_scale": 1.0},
        "priyamvada": {"model": "hi_IN-priyamvada-medium.onnx",
                       "label": "Priyamvada", "length_scale": 1.0},
    },
}
AUDIO_DIR = Path(os.environ.get(
    "AUDIO_DIR", Path(__file__).resolve().parent.parent / "audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
# erlaubte Zeichen: Latein (inkl. Akzente), arabisches Alphabet,
# Kyrillisch, CJK, Hiragana/Katakana, Devanagari,
# Apostrophe, Bindestrich/Leerzeichen
ANHOEREN_RE = re.compile(
    r"^[\wÄÖÜäöüßÀ-ÿ'’ \-\u0600-\u06FF\u0400-\u04FF"
    r"\u4E00-\u9FFF\u3040-\u30FF\u0900-\u097F"
    r"\uAC00-\uD7A3\u0E00-\u0E7F]{1,100}$", re.UNICODE)
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

app = FastAPI(title="Wörterbuch API", version="1.0.0")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Einfaches in-Memory-Fenster-Limit pro IP (bzw. pro Key, wenn gültig)."""

    def __init__(self, inner):
        super().__init__(inner)
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    async def dispatch(self, request, call_next):
        is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
        key_header = request.headers.get("x-api-key")
        keyed = bool(API_KEY) and key_header == API_KEY
        if keyed:
            bucket = f"key:{key_header}"
            limit = RATE_WRITE_KEYED if is_write else RATE_READ
        else:
            host = request.client.host if request.client else "?"
            bucket = f"{host}:{'w' if is_write else 'r'}"
            limit = RATE_WRITE if is_write else RATE_READ
            # Abstimmen und Kandidaten-Wahl auf der Bewertungsseite sind
            # Writes, sollen aber flüssig klickbar bleiben — eigener Bucket.
            if request.url.path in ("/raten/vote", "/raten/pick",
                                    "/wort/vote", "/raten/move"):
                bucket = f"{host}:v"
                limit = RATE_VOTE
        now = time.monotonic()
        with self.lock:
            dq = self.hits[bucket]
            while dq and dq[0] <= now - WINDOW:
                dq.popleft()
            if len(dq) >= limit:
                return JSONResponse(
                    {"error": f"Rate limit exceeded: {limit} per 1 minute"},
                    status_code=429)
            dq.append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)


# ---------- Schutz für Schreibzugriffe ----------

@app.on_event("startup")
def _announce_key():
    global API_KEY
    if not API_KEY:
        API_KEY = secrets.token_urlsafe(16)
        print(f"\n[woerterbuch] Kein API_KEY gesetzt. Zufälliger Key für diese "
              f"Sitzung: {API_KEY}\n"
              f"[woerterbuch] Schreibzugriffe brauchen den Header "
              f"X-API-Key: <key>\n")


def require_api_key(x_api_key: str | None = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(401, "Ungültiger oder fehlender X-API-Key")


# ---------- Schemas ----------

class EntryIn(BaseModel):
    word: str = Field(min_length=1, max_length=100)
    language: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$",
                          description="Kleinbuchstaben, z. B. 'de', 'en'")
    definition: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="manual", max_length=50)
    script: str = Field(default="", max_length=20,
                        description="Schrift: '' = Standard, 'latn' = Franco")


class EntryOut(BaseModel):
    id: int
    word: str
    language: str
    definition: str | None
    image: str | None = None
    source: str
    created_at: str
    lemma: str | None = None
    form: str | None = None
    confidence: float | None = None
    sprache_unsicher: int = 0
    script: str = ""
    ja: int | None = None
    nein: int | None = None
    evidence: dict | None = None


class VoteIn(BaseModel):
    entry_id: int
    understood: bool
    # Bewertetes Bild (Galerie/Dateiname); None/'' = Hauptbild
    image: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,120}\.(png|webp)$")
    # 'image' = Passt-nicht-Stimme, 'unknown' = "Wort unbekannt"
    kind: str = Field(default="image", pattern=r"^(image|unknown)$")
    # Einmal-Nonce aus /raten/next (oeffentliche Stimmen ohne API-Key)
    nonce: str | None = Field(default=None, max_length=64)


class MoveIn(BaseModel):
    entry_id: int
    filename: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}\.(png|webp)$")
    target_word: str = Field(min_length=1, max_length=100)


class ResolveIn(BaseModel):
    accept: bool
    note: str | None = Field(default=None, max_length=500)


class WordVoteIn(BaseModel):
    entry_id: int
    # Drei Faelle: Gebrauchswort / falsche Sprache / unsicher.
    # fits (bool) bleibt aus Kompatibilitaet erhalten: True -> dazu.
    kind: str | None = Field(default=None, pattern=r"^(dazu|nicht|unsicher)$")
    fits: bool | None = None


class EvidenceIn(BaseModel):
    confidence: float = Field(ge=0, le=1)
    evidence: dict = Field(default_factory=dict)
    sprache_unsicher: bool = False


class ImageIn(BaseModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}\.(png|webp)$")
    data_b64: str = Field(max_length=3_000_000)


class PickIn(BaseModel):
    entry_id: int
    filename: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}\.(png|webp)$")


class BatchIn(BaseModel):
    entries: list[EntryIn] = Field(max_length=500)


class WordFormIn(BaseModel):
    """Reine beobachtete/vorgeschlagene Schreibweise, noch kein Lexem."""
    word: str = Field(min_length=1, max_length=100)
    language: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")
    source: str = Field(default="model-candidate", max_length=50)
    script: str = Field(default="", max_length=20)


class WordFormBatchIn(BaseModel):
    forms: list[WordFormIn] = Field(max_length=500)


class BatchResult(BaseModel):
    inserted: int
    duplicates: int
    errors: int


class LexemeAnalysisIn(BaseModel):
    """Sprachliche Untersuchung, die aus einer Wortform ein Lexem begründet."""
    form_id: int
    lemma: str = Field(min_length=1, max_length=100)
    word_class: str = Field(default="", max_length=50)
    definition: str = Field(min_length=1, max_length=1000)
    source: str = Field(default="model-analysis", max_length=50)


class DefinitionIn(BaseModel):
    definition: str = Field(min_length=1, max_length=1000)


class DefLangIn(BaseModel):
    """Bedeutung in einer bestimmten Sprache setzen (Upsert)."""
    lang: str = Field(min_length=2, max_length=8)
    text: str = Field(min_length=1, max_length=1000)
    source: str = Field(default="model", max_length=40)


class SourceLinkIn(BaseModel):
    """Verifizierten Externlink setzen (Wiktionary/Wikipedia/DWDS)."""
    kind: str = Field(min_length=2, max_length=20)
    url: str = Field(min_length=8, max_length=300)


class WordIn(BaseModel):
    """Wort eines Eintrags korrigieren (Tippfehler, Kodier-Müll)."""
    word: str = Field(min_length=1, max_length=100)


# ---------- Modell-Hilfsfunktionen ----------

def _attrs(row) -> dict:
    """lexemes.attrs-JSON sicher als Dict lesen."""
    try:
        return json.loads(row["attrs"] or "{}")
    except Exception:
        return {}


def _main_image(conn, lexeme_id: int) -> str | None:
    r = conn.execute(
        "SELECT filename FROM lexeme_images"
        " WHERE lexeme_id = ? AND is_main = 1 LIMIT 1", (lexeme_id,)
    ).fetchone()
    return r["filename"] if r else None


def _de_definition(conn, lexeme_id: int) -> str | None:
    r = conn.execute(
        "SELECT text FROM lexeme_defs"
        " WHERE lexeme_id = ? AND lang = 'de' LIMIT 1", (lexeme_id,)
    ).fetchone()
    return r["text"] if r else None


def _entry_from_row(conn, row, with_evidence: bool = True) -> dict:
    """word_forms+lexemes-Zeile als altes EntryOut-Dict aufbereiten.

    Migration ist ID-erhaltend: w.id == lexeme_id == alte entries.id.
    """
    lex_id = row["id"]
    d = {
        "id": lex_id,
        "word": row["word"],
        "language": row["language"],
        "definition": _de_definition(conn, lex_id),
        "image": _main_image(conn, lex_id),
        "source": row["source"],
        "created_at": row["created_at"],
        "lemma": row["lemma"],
        "form": _attrs(row).get("form") or None,
        "confidence": row["confidence"],
        "sprache_unsicher": row["sprache_unsicher"],
        "script": row["script"] or "",
        "ja": None,
        "nein": None,
        "evidence": None,
    }
    if with_evidence:
        ev = row["evidence"]
        if isinstance(ev, str):
            try:
                d["evidence"] = json.loads(ev or "null")
            except Exception:
                d["evidence"] = None
    return d


_ENTRY_SELECT = (
    "SELECT w.id, w.word, w.language, w.script, w.source, w.created_at,"
    " l.lemma, l.word_class, l.status, l.attrs, l.confidence, l.evidence,"
    " l.sprache_unsicher"
    " FROM word_forms w JOIN lexemes l ON l.id = w.id"
)


def _fetch_existing(language: str, words: list[str]) -> set[str]:
    conn = get_conn()
    try:
        marks = ",".join("?" for _ in words)
        rows = conn.execute(
            f"SELECT word FROM word_forms WHERE language = ? "
            f"AND word IN ({marks})", [language, *words],
        ).fetchall()
        return {r["word"] for r in rows}
    finally:
        conn.close()


def _count_search(term: str, language: str) -> None:
    """Suchbegriff anonym mitzählen (für 'Meistgesucht' auf der Startseite)."""
    term = term.strip().lower()
    if not 2 <= len(term) <= 100:
        return
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO search_stats (term, language, count) VALUES (?, ?, 1) "
            "ON CONFLICT (term, language) DO UPDATE SET count = count + 1",
            (term, language.lower()),
        )
        conn.commit()
    finally:
        conn.close()


def _set_main_image(conn, lexeme_id: int, filename: str) -> None:
    """Hauptbild setzen: genau eine is_main=1-Zeile pro Lexem."""
    conn.execute(
        "UPDATE lexeme_images SET is_main = 0 WHERE lexeme_id = ?", (lexeme_id,))
    conn.execute(
        "INSERT OR IGNORE INTO lexeme_images (lexeme_id, filename, is_main)"
        " VALUES (?, ?, 0)", (lexeme_id, filename))
    conn.execute(
        "UPDATE lexeme_images SET is_main = 1"
        " WHERE lexeme_id = ? AND filename = ?", (lexeme_id, filename))


def _find_word(conn, word: str, language: str):
    return conn.execute(
        f"{_ENTRY_SELECT} WHERE w.word = ? AND w.language = ?"
        " AND l.status <> 'candidate'",
        (word, language.lower()),
    ).fetchone()


# ---------- Endpunkte: statische Seiten ----------

@app.get("/", include_in_schema=False)
def index():
    """Startseite für Menschen (die API-Doku liegt unter /docs)."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/verzeichnis", include_in_schema=False)
def verzeichnis():
    """Alphabetisches Stichwortverzeichnis zum Blättern."""
    return FileResponse(Path(__file__).parent / "static" / "verzeichnis.html")


@app.get("/impressum", include_in_schema=False)
def impressum():
    return FileResponse(Path(__file__).parent / "static" / "impressum.html")


@app.get("/datenschutz", include_in_schema=False)
def datenschutz():
    return FileResponse(Path(__file__).parent / "static" / "datenschutz.html")


@app.get("/basis.css", include_in_schema=False)
def basis_css():
    """Gemeinsames Designsystem (Farben, Typografie, Buttons, Formulare)."""
    return FileResponse(Path(__file__).parent / "static" / "basis.css",
                        media_type="text/css")


@app.get("/wb.js", include_in_schema=False)
def wb_js():
    """Gemeinsame Wörterbuch-Funktionen (siehe FUNKTIONSKATALOG.md)."""
    return FileResponse(Path(__file__).parent / "static" / "wb.js",
                        media_type="application/javascript")


@app.get("/tlh-grammatik.json", include_in_schema=False)
def tlh_grammatik_json():
    """Klingonische Affix-Tabellen (boQwI' book, Apache-2.0) fuer die
    Wortseite — kanonische Grammatik statt Modellgenerierung."""
    return FileResponse(Path(__file__).parent / "static" /
                        "tlh-grammatik.json",
                        media_type="application/json")


@app.get("/worteintrag", include_in_schema=False)
def worteintrag():
    """Wort-Detailseite: vollständig generisch aus /wortdaten aufgebaut."""
    return FileResponse(Path(__file__).parent / "static" / "wort.html")


@app.get("/bewerten", include_in_schema=False)
def bewerten():
    """Seite zum Bewerten der Wort-Bilder."""
    return FileResponse(Path(__file__).parent / "static" / "bewerten.html")


@app.get("/sprecher", include_in_schema=False)
def sprecher_seite():
    """Sprecher-Testseite: Stimmen anhören, Tempo testen, bewerten."""
    return FileResponse(Path(__file__).parent / "static" / "sprecher.html")


# ---------- Einträge (Adapter: word_forms + lexemes, IDs identisch) ----------

def _create_entry(conn, word: str, language: str, definition: str | None,
                  source: str, script: str) -> int:
    """Kompatibilitätspfad für bereits untersuchte Einträge.

    Neue Ernte legt zunächst nur Wortformen über /word-forms/batch an.
    Dieser alte Pfad erzeugt weiterhin ein Lexem, verknüpft die Identitäten
    aber ausdrücklich über Lesart und Formbeziehung.
    """
    cur = conn.execute(
        "INSERT INTO word_forms (word, language, source, script)"
        " VALUES (?, ?, ?, ?)",
        (word, language, source, script),
    )
    new_id = cur.lastrowid
    conn.execute(
        "INSERT INTO lexemes (id, language, lemma, source)"
        " VALUES (?, ?, ?, ?)",
        (new_id, language, word, source),
    )
    conn.execute(
        "INSERT INTO readings (form_id, lexeme_id, source)"
        " VALUES (?, ?, ?)", (new_id, new_id, source))
    conn.execute(
        "INSERT INTO lexeme_forms (lexeme_id, form_id, cell)"
        " VALUES (?, ?, '')", (new_id, new_id))
    if definition:
        conn.execute(
            "INSERT OR REPLACE INTO lexeme_defs (lexeme_id, lang, text, source)"
            " VALUES (?, 'de', ?, ?)", (new_id, definition, source))
    return new_id


@app.post("/entries", response_model=EntryOut, status_code=201)
def create_entry(entry: EntryIn, _=Depends(require_api_key)):
    conn = get_conn()
    try:
        try:
            _create_entry(conn, entry.word.strip(), entry.language.lower(),
                          entry.definition, entry.source, entry.script)
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise HTTPException(
                409, f"'{entry.word}' existiert bereits ({entry.language})")
        row = _find_word(conn, entry.word.strip(), entry.language)
        return _entry_from_row(conn, row)
    finally:
        conn.close()


@app.post("/entries/batch", response_model=BatchResult)
def create_batch(batch: BatchIn, _=Depends(require_api_key)):
    """Viele Einträge auf einmal. Duplikate (Wort+Sprache) werden übersprungen."""
    inserted = duplicates = errors = 0
    by_lang: dict[str, list[EntryIn]] = {}
    for e in batch.entries:
        by_lang.setdefault(e.language.lower(), []).append(e)
    conn = get_conn()
    try:
        for lang, items in by_lang.items():
            # Duplikat-Check auf DIESER Connection (kein verschachtelter
            # get_conn — zwei Connections mit Migrations-DML blockieren
            # sich sonst gegenseitig: database is locked).
            words = [e.word.strip() for e in items]
            marks = ",".join("?" for _ in words)
            rows = conn.execute(
                f"SELECT word FROM word_forms WHERE language = ? "
                f"AND word IN ({marks})", [lang, *words],
            ).fetchall()
            existing = {r["word"] for r in rows}
            for e in items:
                word = e.word.strip()
                if word in existing:
                    duplicates += 1
                    continue
                try:
                    _create_entry(conn, word, lang, e.definition,
                                  e.source, e.script)
                    existing.add(word)
                    inserted += 1
                except Exception:
                    errors += 1
        conn.commit()
    finally:
        conn.close()
    return BatchResult(inserted=inserted, duplicates=duplicates, errors=errors)


@app.post("/word-forms/batch", response_model=BatchResult)
def create_word_forms(batch: WordFormBatchIn, _=Depends(require_api_key)):
    """Schreibweisen sammeln, ohne daraus automatisch Lexeme zu behaupten."""
    inserted = duplicates = errors = 0
    conn = get_conn()
    try:
        for item in batch.forms:
            word = item.word.strip()
            lang = item.language.lower()
            exists = conn.execute(
                "SELECT id FROM word_forms WHERE language = ? AND word = ?"
                " AND script = ?", (lang, word, item.script)).fetchone()
            if exists:
                duplicates += 1
                continue
            try:
                conn.execute(
                    "INSERT INTO word_forms"
                    " (language, word, script, source) VALUES (?, ?, ?, ?)",
                    (lang, word, item.script, item.source))
                inserted += 1
            except Exception:
                errors += 1
        conn.commit()
        return BatchResult(inserted=inserted, duplicates=duplicates,
                           errors=errors)
    finally:
        conn.close()


@app.get("/word-forms")
def list_word_forms(
    language: str,
    prefix: str | None = None,
    unanalyzed: bool = False,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
):
    """Wortformen unabhängig von einer vorhandenen lexikalischen Analyse."""
    sql = (
        "SELECT w.id AS form_id, w.word, w.language, w.script, w.source,"
        " w.created_at,"
        " CASE WHEN EXISTS (SELECT 1 FROM readings r"
        "  JOIN lexemes rl ON rl.id = r.lexeme_id"
        "  WHERE r.form_id = w.id AND rl.status <> 'candidate')"
        " THEN 1 ELSE 0 END AS analyzed"
        " FROM word_forms w WHERE w.language = ?"
    )
    params: list = [language.lower()]
    if prefix:
        sql += " AND ULOWER(w.word) LIKE ULOWER(?)"
        params.append(prefix + "%")
    if unanalyzed:
        sql += (" AND NOT EXISTS (SELECT 1 FROM readings r"
                " JOIN lexemes rl ON rl.id = r.lexeme_id"
                " WHERE r.form_id = w.id AND rl.status <> 'candidate')")
    sql += " ORDER BY w.word LIMIT ? OFFSET ?"
    params += [limit, offset]
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@app.post("/lexemes/analyze", status_code=201)
def analyze_word_form(body: LexemeAnalysisIn, _=Depends(require_api_key)):
    """Nach sprachlicher Untersuchung ein Lexem zu einer Wortform anlegen."""
    conn = get_conn()
    try:
        form = conn.execute(
            "SELECT id, word, language FROM word_forms WHERE id = ?",
            (body.form_id,)).fetchone()
        if not form:
            raise HTTPException(404, f"Wortform {body.form_id} nicht gefunden")
        existing = conn.execute(
            "SELECT r.lexeme_id, l.status FROM readings r"
            " JOIN lexemes l ON l.id = r.lexeme_id"
            " WHERE r.form_id = ? AND r.lexeme_id IS NOT NULL LIMIT 1",
            (body.form_id,)).fetchone()
        if existing and existing["status"] != "candidate":
            raise HTTPException(
                409, f"Wortform {body.form_id} ist bereits analysiert")
        if existing:
            lexeme_id = existing["lexeme_id"]
            conn.execute(
                "UPDATE lexemes SET lemma = ?, word_class = ?,"
                " status = 'provisional', source = ? WHERE id = ?",
                (body.lemma.strip(), body.word_class.strip(), body.source,
                 lexeme_id))
            conn.execute(
                "INSERT OR REPLACE INTO lexeme_defs"
                " (lexeme_id, lang, text, source) VALUES (?, ?, ?, ?)",
                (lexeme_id, form["language"], body.definition.strip(),
                 body.source))
            conn.commit()
            row = _find_word(conn, form["word"], form["language"])
            return _entry_from_row(conn, row)
        # Getrennte Identitäten, aber für die alten Pipeline-Payloads bleibt
        # die numerische ID bei neu analysierten Trägerformen stabil.
        collision = conn.execute(
            "SELECT id FROM lexemes WHERE id = ?", (body.form_id,)).fetchone()
        if collision:
            raise HTTPException(
                409, f"Lexem-ID {body.form_id} ist bereits belegt")
        conn.execute(
            "INSERT INTO lexemes"
            " (id, language, lemma, word_class, status, source)"
            " VALUES (?, ?, ?, ?, 'provisional', ?)",
            (body.form_id, form["language"], body.lemma.strip(),
             body.word_class.strip(), body.source))
        conn.execute(
            "INSERT INTO readings (form_id, lexeme_id, source)"
            " VALUES (?, ?, ?)", (body.form_id, body.form_id, body.source))
        conn.execute(
            "INSERT INTO lexeme_forms (lexeme_id, form_id, cell)"
            " VALUES (?, ?, '')", (body.form_id, body.form_id))
        conn.execute(
            "INSERT INTO lexeme_defs (lexeme_id, lang, text, source)"
            " VALUES (?, ?, ?, ?)",
            (body.form_id, form["language"], body.definition.strip(),
             body.source))
        conn.commit()
        row = _find_word(conn, form["word"], form["language"])
        return _entry_from_row(conn, row)
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/entries", response_model=list[EntryOut])
def list_entries(
    language: str | None = None,
    prefix: str | None = Query(default=None, description="Wörter, die so anfangen"),
    q: str | None = Query(default=None, description="Teilstring-Suche im Wort"),
    undefined: bool = Query(default=False,
                            description="nur Einträge ohne Definition"),
    noimage: bool = Query(default=False,
                          description="nur Einträge ohne Bild"),
    nolemma: bool = Query(default=False,
                          description="nur Einträge ohne Grundform"),
    with_votes: bool = Query(default=False,
                             description="Ja/Nein-Bewertungen mit ausgeben"),
    with_evidence: bool = Query(default=False,
                                description="Evidenz-JSON mit ausgeben"),
    neu: bool = Query(default=False,
                      description="neueste Einträge zuerst (nach Anlage)"),
    no_def: str | None = Query(
        default=None,
        description="nur Einträge ohne Definition in dieser Sprache"),
    nolinks: bool = Query(
        default=False,
        description="nur Einträge mit unvollständigen Externquellen-Links"),
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
):
    sql = (_ENTRY_SELECT.replace("SELECT", "SELECT", 1)
           + " WHERE l.status <> 'candidate'")
    params: list = []
    if language:
        sql += " AND w.language = ?"
        params.append(language.lower())
    if prefix:
        # ULOWER(): SQLite foldet nur ASCII, Umlaute (Ö vs. ö) brauchen
        # die in db.py registrierte Unicode-Funktion.
        sql += " AND ULOWER(w.word) LIKE ULOWER(?)"
        params.append(prefix + "%")
    if q:
        sql += " AND (w.word LIKE ?"
        params.append(f"%{q}%")
        # Franco-Eingabe? Zusätzlich die arabischen Kandidaten suchen
        for v in franco_variants(q):
            sql += " OR w.word LIKE ?"
            params.append(f"%{v}%")
        # ... und gespeicherte latn-Formen mit gleichem Skeleton
        for v in franco_latn_matches(q):
            sql += " OR w.word = ?"
            params.append(v)
        sql += ")"
        _count_search(q, language or "")
    if undefined:
        sql += (" AND NOT EXISTS (SELECT 1 FROM lexeme_defs d"
                " WHERE d.lexeme_id = w.id AND d.lang = 'de')")
    if noimage:
        sql += (" AND NOT EXISTS (SELECT 1 FROM lexeme_images li"
                " WHERE li.lexeme_id = w.id)")
    if nolemma:
        sql += " AND (l.lemma IS NULL OR l.lemma = '')"
    if no_def:
        dl = no_def.lower()
        sql += (" AND NOT EXISTS (SELECT 1 FROM lexeme_defs d"
                " WHERE d.lexeme_id = w.id AND d.lang = ?)")
        params.append(dl)
    if nolinks:
        # Vollständig = alle Wikis + Wikidata (+ DWDS bei de). Unvollständige
        # nachrüsten. tn hat kein Wiki/Wikidata -> 0 erwartet.
        l = (language or "").lower()
        erwartet = 4 if l == "de" else (0 if l == "tn" else 3)
        sql += (" AND (SELECT COUNT(*) FROM source_links s"
                " WHERE s.lexeme_id = w.id) < ?")
        params.append(erwartet)
    sql += " ORDER BY w.id DESC" if neu else " ORDER BY w.word"
    sql += " LIMIT ? OFFSET ?"
    params += [limit, offset]
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        out = [_entry_from_row(conn, r, with_evidence=with_evidence)
               for r in rows]
        if with_votes and out:
            marks = ",".join("?" for _ in out) or "NULL"
            votes = conn.execute(
                f"SELECT lexeme_id, "
                f"SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja, "
                f"SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein "
                f"FROM ratings WHERE lexeme_id IN ({marks}) "
                f"AND kind = 'image' GROUP BY lexeme_id",
                [e["id"] for e in out],
            ).fetchall()
            vmap = {v["lexeme_id"]: (v["ja"] or 0, v["nein"] or 0)
                    for v in votes}
            for e in out:
                e["ja"], e["nein"] = vmap.get(e["id"], (0, 0))
        return out
    finally:
        conn.close()


@app.get("/lookup", response_model=EntryOut)
def lookup(word: str, language: str):
    """Exaktes Nachschlagen eines Wortes in einer Sprache.

    Fallback: Franco-Eingabe (sbe7) findet auch die arabische Schreibweise.
    """
    _count_search(word, language)
    conn = get_conn()
    try:
        row = _find_word(conn, word.strip(), language)
        if not row:
            for v in franco_variants(word):
                row = _find_word(conn, v, language)
                if row:
                    break
        if not row:
            # Skeleton: sbe7 findet die gespeicherte Form sba7
            for v in franco_latn_matches(word):
                row = _find_word(conn, v, language)
                if row:
                    break
        if not row:
            raise HTTPException(404, f"'{word}' nicht gefunden ({language})")
        return _entry_from_row(conn, row)
    finally:
        conn.close()


# ---------- Franco-Arabisch (Tunesisch in Lateinschrift) ----------
# Alltagsschrift Tunesiens: lateinische Buchstaben + Ziffern für Laute,
# die das lateinische Alphabet nicht hat (3=ع, 7=ح, 5=خ, 9=ق, 2=ء).
# "sbe7" -> صباح. Nur aktiviert, wenn Ziffern vorkommen — reine
# Latin-Eingaben ohne Ziffern sind mehrdeutig und bleiben unangetastet.
_FRANCO_DIGRAPHS = [("ch", "ش"), ("sh", "ش"), ("th", "ث"), ("kh", "خ"),
                    ("dh", "ذ"), ("gh", "غ"), ("ph", "ف")]
_FRANCO_DIGITS = {"2": "ء", "3": "ع", "5": "خ", "7": "ح", "9": "ق"}
# Volltransliteration Latin -> Arabisch (Maghreb-Konvention).
_FRANCO_LETTERS = {
    "a": "ا", "b": "ب", "c": "ك", "d": "د", "f": "ف", "g": "ج",
    "h": "ه", "i": "ي", "j": "ج", "k": "ك", "l": "ل", "m": "م",
    "n": "ن", "o": "و", "p": "ب", "q": "ق", "r": "ر", "s": "س",
    "t": "ت", "u": "و", "v": "ف", "w": "و", "x": "كس", "y": "ي",
    "z": "ز", "e": "ي", "é": "ي", "è": "ي", "ø": "و",
}


def franco_variants(query: str) -> list[str]:
    """Arabisch-Schrift-Kandidaten für eine Franco-Eingabe.

    Liefert [] wenn keine Franco-Ziffer enthalten ist. Mehrere Kandidaten:
    1) Ziffern/Digraphen nur ersetzt (deterministisch),
    2) Volltransliteration (z. B. "sbe7" -> "سبيح"),
    3) Konsonanten-Skeleton davon, vowel-los (س ب ح) — robust gegen
       unterschiedliche Vokal-Schreibweisen.
    """
    if not any(c in query for c in _FRANCO_DIGITS):
        return []
    t = query.lower()
    for lat, ar in _FRANCO_DIGRAPHS:
        t = t.replace(lat, ar)
    for digit, ar in _FRANCO_DIGITS.items():
        t = t.replace(digit, ar)
    variants = {t}
    if t.startswith("ء"):
        variants.add("أ" + t[1:])
    # Volltransliteration ueber die noch verbliebenen Lateinbuchstaben
    full = "".join(_FRANCO_LETTERS.get(c, c) for c in t)
    variants.add(full)
    # Konsonanten-Skeleton: Vokale beidseitig ignorieren (ا و ي ء)
    skeleton = "".join(c for c in full if c not in "اويء")
    variants.add(skeleton)
    return sorted(v for v in variants if v and v != query.lower())


# --- Skeleton-Matching gegen gespeicherte Franco-Einträge ---------------
# Vokal-Schreibweisen schwanken (sbe7 vs. sba7). Wörter mit derselben
# Konsonant-Ziffer-Kette gelten als gleich; abgeglichen wird gegen die
# in der DB gespeicherten latn-Formen (kleine Mengen, gecacht).
_VOWELS_LATIN = set("aeiou")
_latn_cache: dict = {"ts": 0.0, "words": []}


def _latn_words() -> list[str]:
    now = time.time()
    if now - _latn_cache["ts"] < 60:
        return _latn_cache["words"]
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT word FROM word_forms WHERE script = 'latn'").fetchall()
    finally:
        conn.close()
    _latn_cache.update(ts=now, words=[r["word"] for r in rows])
    return _latn_cache["words"]


def _latn_skeleton(s: str) -> str:
    return "".join(c for c in s.lower() if c not in _VOWELS_LATIN)


def franco_latn_matches(query: str) -> list[str]:
    """Gespeicherte latn-Formen mit gleicher Konsonant-Ziffer-Kette."""
    if not any(c in query for c in _FRANCO_DIGITS):
        return []
    qs = _latn_skeleton(query)
    if len(qs) < 2:
        return []
    return [w for w in _latn_words()
            if _latn_skeleton(w) == qs and w.lower() != query.lower()]


FUGENLAUTE = ("en", "er", "es", "e", "s", "n")


def _split_compound(w: str, vocab: dict, depth: int = 0) -> list | None:
    """Kompositum gegen den Wortbestand zerlegen (längster linker Treffer).

    Zwei Durchgänge: erst exakte linke Treffer, dann Fugenlaut-Varianten
    (sonst "wider-s-tand" statt "wider + stand"). Konservativ:
    Bestandteile mindestens 3 Zeichen, damit keine Scheinzerlegungen
    entstehen.
    """
    for fugen_pass in (False, True):
        for i in range(len(w), 2, -1):
            left, rest = w[:i], w[i:]
            if fugen_pass:
                cands = [left[: -len(f)] for f in FUGENLAUTE
                         if left.endswith(f) and len(left) - len(f) >= 3]
            else:
                cands = [left]
            hit = next((c for c in cands if c in vocab), None)
            if hit is None:
                continue
            if len(rest) >= 3 and rest in vocab:
                return [hit, rest]
            if depth < 2 and len(rest) >= 6:
                sub = _split_compound(rest, vocab, depth + 1)
                if sub and len(sub) >= 2:
                    return [hit] + sub
    return None


@app.get("/zerlegen")
def zerlegen(word: str, language: str):
    """Zerlegt zusammengesetzte Wörter in ihre Bestandteile im Bestand."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT word FROM word_forms WHERE language = ?",
            (language.lower(),),
        ).fetchall()
    finally:
        conn.close()
    vocab = {r["word"].lower(): r["word"] for r in rows}
    w = word.strip().lower()
    if w not in vocab:
        raise HTTPException(404, f"'{word}' nicht gefunden ({language})")
    parts = _split_compound(w, vocab)
    if not parts or len(parts) < 2:
        return {"word": word, "parts": []}
    return {"word": word,
            "parts": [{"part": p, "entry": vocab[p]} for p in parts]}


# ---------- Wortfamilien / Flexionen ----------

class FormIn(BaseModel):
    word: str = Field(min_length=1, max_length=100)
    language: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")
    lemma: str = Field(min_length=1, max_length=100)
    form: str = Field(min_length=1, max_length=50)


class FormBatchIn(BaseModel):
    items: list[FormIn] = Field(max_length=500)


@app.get("/formen")
def formen(word: str, language: str):
    """Alle bekannten Formen derselben Wortfamilie (gleiche Grundform)."""
    lang = language.lower()
    conn = get_conn()
    try:
        row = _find_word(conn, word.strip(), lang)
        if not row:
            raise HTTPException(404, f"'{word}' nicht gefunden ({lang})")
        lemma = row["lemma"] or row["word"]
        rows = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.language = ?"
            " AND (l.lemma = ? OR w.word = ?) ORDER BY w.word",
            (lang, lemma, lemma),
        ).fetchall()
        forms = [{"word": r["word"],
                  "form": _attrs(r).get("form") or "",
                  "script": r["script"] or ""} for r in rows]
        return {"word": row["word"], "lemma": lemma, "forms": forms}
    finally:
        conn.close()


@app.post("/formen/batch")
def formen_batch(batch: FormBatchIn, _=Depends(require_api_key)):
    """Grundform/Flexionsform für viele Einträge setzen (für den Harvester)."""
    updated = missing = 0
    conn = get_conn()
    try:
        for it in batch.items:
            row = conn.execute(
                "SELECT w.id, l.attrs FROM word_forms w JOIN lexemes l"
                " ON l.id = w.id"
                " WHERE w.word = ? AND w.language = ?",
                (it.word.strip(), it.language.lower()),
            ).fetchone()
            if not row:
                missing += 1
                continue
            attrs = _attrs(row)
            attrs["form"] = it.form.strip()
            cur = conn.execute(
                "UPDATE lexemes SET lemma = ?, attrs = ? WHERE id = ?",
                (it.lemma.strip(), json.dumps(attrs, ensure_ascii=False),
                 row["id"]),
            )
            if cur.rowcount:
                updated += 1
            else:
                missing += 1
        conn.commit()
    finally:
        conn.close()
    return {"updated": updated, "missing": missing}


# ---------- Grammatik (Wortart, Genus, Flexion) ----------

class GrammarIn(BaseModel):
    language: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")
    lemma: str = Field(min_length=1, max_length=100)
    word_class: str | None = Field(default=None, max_length=50)
    gender: str | None = Field(default=None, max_length=20)
    article: str | None = Field(default=None, max_length=10)
    flexion: dict | None = None


class GrammarBatchIn(BaseModel):
    items: list[GrammarIn] = Field(max_length=500)


def _flexion_nest(conn, lexeme_id: int) -> dict | None:
    """lexeme_forms-Zellen als geschachteltes Dict (altes Flexion-Format)."""
    rows = conn.execute(
        "SELECT lf.cell, w.word FROM lexeme_forms lf"
        " JOIN word_forms w ON w.id = lf.form_id"
        " WHERE lf.lexeme_id = ? ORDER BY lf.id", (lexeme_id,),
    ).fetchall()
    if not rows:
        return None
    root: dict = {}
    for r in rows:
        try:
            path = json.loads(r["cell"] or "[]")
        except Exception:
            path = []
        if not path:
            continue
        node = root
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = r["word"]
    return root or None


def _grammar_for_lexeme(conn, row) -> dict:
    attrs = _attrs(row)
    return {"language": row["language"], "lemma": row["lemma"],
            "word_class": row["word_class"] or None,
            "gender": attrs.get("genus"),
            "article": attrs.get("article"),
            "flexion": _flexion_nest(conn, row["id"])}


@app.get("/grammatik")
def grammatik(word: str, language: str):
    """Grammatik-Informationen des Lemmas eines Wortes.

    Aufloesung: lemma des Wortes, Fallback das Wort selbst — so sehen
    Flexionsformen (Kantone) dieselbe Grammatik wie die Grundform (Kanton).
    """
    lang = language.lower()
    conn = get_conn()
    try:
        row = _find_word(conn, word.strip(), lang)
        lemma = (row["lemma"] or word.strip()) if row else word.strip()
        g = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.language = ? AND l.lemma = ?"
            " ORDER BY w.id LIMIT 1",
            (lang, lemma),
        ).fetchone()
        if not g:
            raise HTTPException(404, f"Keine Grammatik für '{word}' ({lang})")
        return _grammar_for_lexeme(conn, g)
    finally:
        conn.close()


def _lexem_ids_fuer_lemma(conn, language: str, lemma: str) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM lexemes WHERE language = ? AND lemma = ?",
        (language, lemma),
    ).fetchall()]


@app.post("/grammatik/batch")
def grammatik_batch(batch: GrammarBatchIn, _=Depends(require_api_key)):
    """Grammatik fuer viele Lemmata setzen (Harvest-Pipeline).

    Schreibt auf alle Lexeme mit diesem Lemma (Wortfamilie); das Flexions-
    Paradigma ersetzt die bisherigen Zellen dieser Lexeme.
    """
    conn = get_conn()
    updated = new = 0
    try:
        for it in batch.items:
            lang = it.language.lower()
            lemma = it.lemma.strip()
            lex_ids = _lexem_ids_fuer_lemma(conn, lang, lemma)
            if not lex_ids:
                conn.execute(
                    "INSERT INTO lexemes (language, lemma, word_class,"
                    " source, status) VALUES (?, ?, ?, 'model',"
                    " 'provisional')",
                    (lang, lemma, it.word_class or ""))
                new += 1
                lex_ids = _lexem_ids_fuer_lemma(conn, lang, lemma)
            else:
                updated += 1
            for lex_id in lex_ids:
                row = conn.execute(
                    "SELECT attrs FROM lexemes WHERE id = ?", (lex_id,)
                ).fetchone()
                attrs = _attrs(row)
                if it.gender is not None:
                    attrs["genus"] = it.gender
                if it.article is not None:
                    attrs["article"] = it.article
                attrs["grammar_source"] = "model"
                conn.execute(
                    "UPDATE lexemes SET word_class = ?, attrs = ? WHERE id = ?",
                    (it.word_class or "", attrs and
                     json.dumps(attrs, ensure_ascii=False) or "", lex_id))
                # Paradigma ersetzen: alte Zellen weg, neue aus dem Dict
                conn.execute(
                    "DELETE FROM lexeme_forms WHERE lexeme_id = ?", (lex_id,))
                if it.flexion:
                    def walk(node, path):
                        if isinstance(node, dict):
                            for k, v in node.items():
                                walk(v, path + [str(k)])
                        elif isinstance(node, str) and node.strip():
                            wf = conn.execute(
                                "SELECT id FROM word_forms"
                                " WHERE language = ? AND word = ?",
                                (lang, node),
                            ).fetchone()
                            if not wf:
                                cur = conn.execute(
                                    "INSERT INTO word_forms (language, word,"
                                    " source) VALUES (?, ?, 'paradigm')",
                                    (lang, node))
                                wf_id = cur.lastrowid
                            else:
                                wf_id = wf["id"]
                            conn.execute(
                                "INSERT OR IGNORE INTO lexeme_forms"
                                " (lexeme_id, form_id, cell) VALUES (?, ?, ?)",
                                (lex_id, wf_id,
                                 json.dumps(path, ensure_ascii=False)))
                    walk(it.flexion, [])
        conn.commit()
    finally:
        conn.close()
    return {"updated": updated, "new": new}


@app.get("/languages")
def languages():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT language, COUNT(*) AS count FROM word_forms"
            " GROUP BY language ORDER BY language"
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


_stats_cache: dict = {"ts": 0.0, "data": {}}


@app.get("/stats")
def stats():
    now = time.time()
    if now - _stats_cache["ts"] < 30:
        return _stats_cache["data"]
    conn = get_conn()
    try:
        total_word_forms = conn.execute(
            "SELECT COUNT(*) AS n FROM word_forms").fetchone()["n"]
        total_lexemes = conn.execute(
            "SELECT COUNT(*) AS n FROM lexemes"
            " WHERE status <> 'candidate'").fetchone()["n"]
        pending_forms = conn.execute(
            "SELECT COUNT(*) AS n FROM word_forms w"
            " WHERE NOT EXISTS (SELECT 1 FROM readings r"
            " JOIN lexemes l ON l.id = r.lexeme_id"
            " WHERE r.form_id = w.id AND l.status <> 'candidate')"
        ).fetchone()["n"]
        with_image = conn.execute(
            "SELECT COUNT(DISTINCT lexeme_id) AS n FROM lexeme_images"
        ).fetchone()["n"]
        with_definition = conn.execute(
            "SELECT COUNT(DISTINCT lexeme_id) AS n FROM lexeme_defs"
            " WHERE lang = 'de'").fetchone()["n"]
        # Tageszuwachs: String-Vergleich funktioniert in beiden Dialekten,
        # solange das Format YYYY-MM-DD HH:MM:SS bleibt.
        heute = time.strftime("%Y-%m-%d")
        today_new = conn.execute(
            "SELECT COUNT(*) AS n FROM lexemes"
            " WHERE status <> 'candidate' AND created_at >= ?",
            (heute,)).fetchone()["n"]
    finally:
        conn.close()
    try:
        with_audio = sum(1 for _ in AUDIO_DIR.glob("*.wav"))
    except Exception:
        with_audio = 0
    _stats_cache.update(ts=now, data={
        "total_entries": total_lexemes,
        "total_lexemes": total_lexemes,
        "total_word_forms": total_word_forms,
        "pending_word_forms": pending_forms,
        "with_image": with_image,
        "with_definition": with_definition,
        "with_audio": with_audio,
        "today_new": today_new,
    })
    return _stats_cache["data"]


@app.get("/suchen/top")
def suchen_top(limit: int = Query(default=10, le=50)):
    """Die häufigsten Suchbegriffe (anonym gezählt)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT term, language, count FROM search_stats "
            "ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.patch("/entries/{entry_id}", response_model=EntryOut)
def set_definition(entry_id: int, body: DefinitionIn,
                   _=Depends(require_api_key)):
    """Definition eines Eintrags setzen/überschreiben (z. B. Harvester define).

    Die 'de'-Zeile der lexeme_defs ist die kanonische deutsche Bedeutung."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE lexeme_defs SET text = ? WHERE lexeme_id = ? AND lang = 'de'",
            (body.definition.strip(), entry_id),
        )
        if cur.rowcount == 0:
            exists = conn.execute(
                "SELECT 1 FROM lexemes WHERE id = ?", (entry_id,)).fetchone()
            if not exists:
                raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
            conn.execute(
                "INSERT OR REPLACE INTO lexeme_defs (lexeme_id, lang, text,"
                " source) VALUES (?, 'de', ?, 'harvester')",
                (entry_id, body.definition.strip()))
        conn.commit()
        row = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.id = ?", (entry_id,)
        ).fetchone()
        return _entry_from_row(conn, row)
    finally:
        conn.close()


@app.get("/entries/{entry_id}/definitions")
def get_definitions(entry_id: int):
    """Bedeutungen eines Eintrags in allen vorhandenen Sprachen
    (Wortebene, unzugeordneter Bestand — lexeme_defs).

    Rückgabe: {"definitions": {"de": "...", "it": "...", ...}}."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM lexemes WHERE id = ?",
                           (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        out: dict[str, str] = {}
        src: dict[str, str] = {}
        for r in conn.execute(
                "SELECT lang, text, source FROM lexeme_defs"
                " WHERE lexeme_id = ? ORDER BY lang",
                (entry_id,)).fetchall():
            out[r["lang"]] = r["text"]
            src[r["lang"]] = r["source"]
        return {"definitions": out, "sources": src}
    finally:
        conn.close()


@app.patch("/entries/{entry_id}/definitions",
           operation_id="patch_definition")
@app.put("/entries/{entry_id}/definitions")
def put_definition(entry_id: int, body: DefLangIn,
                   _=Depends(require_api_key)):
    """Bedeutung in einer Sprache setzen (Upsert; Pipeline/Übersetzung).

    lang = Sprache DES Textes. Wortebenen-Bestand (lexeme_defs)."""
    lang = body.lang.lower()
    text = body.text.strip()
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM lexemes WHERE id = ?",
                           (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        conn.execute(
            "INSERT OR REPLACE INTO lexeme_defs (lexeme_id, lang, text, source)"
            " VALUES (?, ?, ?, ?)",
            (entry_id, lang, text, body.source))
        conn.commit()
        return {"ok": True, "entry_id": entry_id, "lang": lang}
    finally:
        conn.close()


@app.get("/definitions/audit-queue")
def definitions_audit_queue(limit: int = Query(default=50, le=200)):
    """Legacy-Definitionen (als 'de' übernommen, noch nicht geprüft) für das
    autonome Sprachaudit. Älteste zuerst, pro Aufruf ein handlicher Batzen."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT d.lexeme_id AS entry_id, w.language AS language,"
            " w.word AS word, d.text AS text"
            " FROM lexeme_defs d"
            " JOIN lexemes l ON l.id = d.lexeme_id"
            " JOIN word_forms w ON w.id = l.id"
            " WHERE d.lang = 'de' AND d.source = 'legacy'"
            " ORDER BY d.lexeme_id LIMIT ?", (limit,)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


class DefAuditIn(BaseModel):
    action: str           # "confirm" (Text ist wirklich deutsch) |
                          # "move" (Text gehört in eine andere Sprache)
    lang: str = "de"      # Zielsprache bei action="move"
    source: str = "legacy-checked"   # neuer Herkunftshinweis
    clear_entry_def: bool = False    # 'de'-Lexemdefinition entfernen (wenn
                                     # der deutsche Slot frei wird)


@app.post("/entries/{entry_id}/definitions/audit")
def audit_definition(entry_id: int, body: DefAuditIn,
                     _=Depends(require_api_key)):
    """Ergebnis des Sprachaudits für die 'de'-Definition eines Eintrags.

    confirm: nur Herkunft setzen. move: Zeile auf die wahre Sprache
    umbiegen (Kollision → als Dublette markieren statt überschreiben).
    """
    action = body.action.strip().lower()
    if action not in ("confirm", "move"):
        raise HTTPException(422, "action muss 'confirm' oder 'move' sein")
    to_lang = body.lang.strip().lower()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM lexeme_defs WHERE lexeme_id = ? AND lang = 'de'",
            (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, "keine 'de'-Definition zu diesem Eintrag")
        if action == "confirm":
            conn.execute(
                "UPDATE lexeme_defs SET source = ?"
                " WHERE lexeme_id = ? AND lang = 'de'",
                (body.source, entry_id))
        else:
            clash = conn.execute(
                "SELECT id FROM lexeme_defs WHERE lexeme_id = ? AND lang = ?",
                (entry_id, to_lang)).fetchone()
            if clash:
                conn.execute(
                    "UPDATE lexeme_defs SET source = 'legacy-dup'"
                    " WHERE lexeme_id = ? AND lang = 'de'",
                    (entry_id,))
            else:
                conn.execute(
                    "UPDATE lexeme_defs SET lang = ?, source = ?"
                    " WHERE lexeme_id = ? AND lang = 'de'",
                    (to_lang, body.source, entry_id))
            if body.clear_entry_def:
                conn.execute(
                    "DELETE FROM lexeme_defs WHERE lexeme_id = ? AND lang = 'de'",
                    (entry_id,))
        conn.commit()
        return {"ok": True, "entry_id": entry_id, "action": action}
    finally:
        conn.close()


# ---------- Sinn-Ebene: Wort -> Bedeutung -> Übersetzung ----------

class SenseIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    word_class: str = Field(default="", max_length=40)
    source: str = Field(default="manual", max_length=40)


class SenseDefIn(BaseModel):
    lang: str = Field(min_length=2, max_length=8)
    text: str = Field(min_length=1, max_length=1000)
    source: str = Field(default="model", max_length=40)


class SenseTransIn(BaseModel):
    entry_id: int
    approx: bool = False  # ungefähre Entsprechung (kein 1:1-Äquivalent)
    source: str = Field(default="model", max_length=40)
    # Optional: konkrete Ziel-Bedeutung, falls der Zieleintrag mehrere
    # Bedeutungen hat (409-Auflösung durch die Pipeline anhand
    # sprachlicher Belege; keine erfundene Zuordnung ohne dieses Feld).
    target_sense_id: int | None = None


class SenseLinkIn(BaseModel):
    kind: str = Field(min_length=2, max_length=16)
    url: str
    claim: str = Field(default="", max_length=190)
    source: str = Field(default="manual", max_length=40)


class SenseImageIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    assign: bool = True  # False = Zuordnung aufheben


class SenseReviewIn(BaseModel):
    valid: bool
    label: str = Field(default="", max_length=200)
    word_class: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=1000)
    source: str = Field(default="sense-review", max_length=40)


def _senses_fuer_eintrag(conn, entry_id: int) -> list[dict]:
    """Alle Bedeutungen eines Lexems, verschachtelt mit Beschreibungen,
    Übersetzungszielen und Belegen (meanings-Modell)."""
    sinne = conn.execute(
        "SELECT id, label, word_class, source, status FROM meanings"
        " WHERE lexeme_id = ? AND status <> 'rejected' ORDER BY id",
        (entry_id,)).fetchall()
    out = []
    for s in sinne:
        sid = s["id"]
        defs = conn.execute(
            "SELECT lang, text, source FROM meaning_definitions"
            " WHERE meaning_id = ?", (sid,)).fetchall()
        trans = conn.execute(
            "SELECT m2.lexeme_id AS entry_id, w.word AS word,"
            " w.language AS language, t.approx AS approx,"
            " t.source AS source"
            " FROM meaning_translations t"
            " JOIN meanings m2 ON m2.id = t.to_meaning_id"
            " JOIN word_forms w ON w.id = m2.lexeme_id"
            " WHERE t.from_meaning_id = ? ORDER BY w.language, w.word",
            (sid,)).fetchall()
        links = conn.execute(
            "SELECT kind, url, claim, source FROM meaning_links"
            " WHERE meaning_id = ? ORDER BY kind, claim", (sid,)).fetchall()
        imgs = conn.execute(
            "SELECT filename FROM meaning_images WHERE meaning_id = ?"
            " ORDER BY id", (sid,)).fetchall()
        out.append({
            "id": sid, "label": s["label"],
            "word_class": s["word_class"],
            "source": s["source"],
            "status": s["status"],
            "definitions": {d["lang"]: d["text"] for d in defs},
            "def_sources": {d["lang"]: d["source"] for d in defs},
            "translations": [dict(t) for t in trans],
            "links": [dict(l) for l in links],
            "images": [i["filename"] for i in imgs],
        })
    return out


@app.post("/entries/{entry_id}/senses", status_code=201)
def add_sense(entry_id: int, body: SenseIn, _=Depends(require_api_key)):
    """Bedeutung (Sinn) zu einem Eintrag anlegen."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM lexemes WHERE id = ?",
                           (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        cur = conn.execute(
            "INSERT INTO meanings (lexeme_id, label, word_class, source)"
            " VALUES (?, ?, ?, ?)",
            (entry_id, body.label.strip(), body.word_class.strip(),
             body.source))
        conn.commit()
        return {"ok": True, "entry_id": entry_id,
                "sense_id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/entries/{entry_id}/senses")
def get_senses(entry_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM lexemes WHERE id = ?",
                           (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        return {"senses": _senses_fuer_eintrag(conn, entry_id)}
    finally:
        conn.close()


@app.get("/senses/review-queue")
def senses_review_queue(
    language: str = Query(min_length=2, max_length=8),
    limit: int = Query(default=50, le=200),
):
    """Vorläufige/externe Bedeutungen der regulären Prüfung zuführen."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.id AS sense_id, m.label, m.word_class, m.source,"
            " d.text, w.word, w.language"
            " FROM meanings m"
            " JOIN lexemes l ON l.id = m.lexeme_id"
            " JOIN word_forms w ON w.id = l.id"
            " LEFT JOIN meaning_definitions d ON d.meaning_id = m.id"
            "  AND d.lang = w.language"
            " WHERE w.language = ? AND m.status = 'provisional'"
            "  AND m.source <> 'translation-carrier'"
            " ORDER BY CASE WHEN m.source = 'manual' THEN 0 ELSE 1 END,"
            " m.id LIMIT ?", (language.lower(), limit)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/senses/image-queue")
def senses_image_queue(
    language: str = Query(min_length=2, max_length=8),
    limit: int = Query(default=100, le=500),
    offset: int = 0,
):
    """Geprüfte Bedeutungen ohne eigenes Bild für die Bildpipeline."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.id AS sense_id, m.lexeme_id AS entry_id, m.label,"
            " d.text, w.word, w.language"
            " FROM meanings m"
            " JOIN lexemes l ON l.id = m.lexeme_id"
            " JOIN word_forms w ON w.id = l.id"
            " JOIN meaning_definitions d ON d.meaning_id = m.id"
            "  AND d.lang = w.language"
            " WHERE w.language = ? AND m.status = 'reviewed'"
            " AND NOT EXISTS (SELECT 1 FROM meaning_images mi"
            "  WHERE mi.meaning_id = m.id)"
            " ORDER BY m.id LIMIT ? OFFSET ?",
            (language.lower(), limit, offset)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.patch("/senses/{sense_id}/review")
def review_sense(sense_id: int, body: SenseReviewIn,
                 _=Depends(require_api_key)):
    """Prüfergebnis erhalten; widerlegte Sinne bleiben als rejected erhalten."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT m.id, l.language FROM meanings m"
            " JOIN lexemes l ON l.id = m.lexeme_id WHERE m.id = ?",
            (sense_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Sinn {sense_id} nicht gefunden")
        if body.valid:
            conn.execute(
                "UPDATE meanings SET label = ?, word_class = ?,"
                " status = 'reviewed', source = ? WHERE id = ?",
                (body.label.strip(), body.word_class.strip(), body.source,
                 sense_id))
            if body.description.strip():
                conn.execute(
                    "INSERT OR REPLACE INTO meaning_definitions"
                    " (meaning_id, lang, text, source) VALUES (?, ?, ?, ?)",
                    (sense_id, row["language"], body.description.strip(),
                     body.source))
        else:
            conn.execute(
                "UPDATE meanings SET status = 'rejected', source = ?"
                " WHERE id = ?", (body.source, sense_id))
        conn.commit()
        return {"ok": True, "sense_id": sense_id,
                "status": "reviewed" if body.valid else "rejected"}
    finally:
        conn.close()


@app.post("/senses/{sense_id}/images")
def sense_image_zuordnen(sense_id: int, body: SenseImageIn,
                         _=Depends(require_api_key)):
    """Bild einer Bedeutung zuordnen (assign=True) oder Zuordnung aufheben.

    Die Datei muss dem Lexem des Sinns bereits gehören (Hauptbild oder
    Galerie) — die Zuordnung ordnet sie einer Bedeutung zu, sie ändert
    nicht den Bildbestand.
    """
    conn = get_conn()
    try:
        s = conn.execute(
            "SELECT id, lexeme_id FROM meanings WHERE id = ?",
            (sense_id,)).fetchone()
        if not s:
            raise HTTPException(404, f"Sinn {sense_id} nicht gefunden")
        gehoert = conn.execute(
            "SELECT 1 FROM lexeme_images WHERE lexeme_id = ? AND filename = ?",
            (s["lexeme_id"], body.filename)).fetchone()
        if not gehoert:
            raise HTTPException(
                404, f"Bild {body.filename!r} gehört nicht zum Eintrag")
        if body.assign:
            conn.execute(
                "INSERT OR IGNORE INTO meaning_images"
                " (meaning_id, filename) VALUES (?, ?)",
                (sense_id, body.filename))
        else:
            conn.execute(
                "DELETE FROM meaning_images"
                " WHERE meaning_id = ? AND filename = ?",
                (sense_id, body.filename))
        conn.commit()
        return {"ok": True, "sense_id": sense_id,
                "filename": body.filename, "assign": body.assign}
    finally:
        conn.close()


@app.get("/senses/queue")
def senses_queue(language: str = Query(min_length=2, max_length=8),
                 limit: int = Query(default=50, le=200)):
    """Einträge mit Definition, aber noch ohne Bedeutungsebenen — Futter für
    die autonome Sinntrennung."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT l.id AS id, w.word AS word, w.language AS language,"
            " d.text AS definition"
            " FROM lexemes l"
            " JOIN word_forms w ON w.id = l.id"
            " JOIN lexeme_defs d ON d.lexeme_id = l.id AND d.lang = 'de'"
            " WHERE w.language = ?"
            " AND NOT EXISTS (SELECT 1 FROM meanings m"
            "  WHERE m.lexeme_id = l.id)"
            " ORDER BY l.id LIMIT ?", (language.lower(), limit)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/senses/translate-queue")
def senses_translate_queue(frm: str = Query(min_length=2, max_length=8,
                                            alias="from"),
                           to: str = Query(min_length=2, max_length=8),
                           limit: int = Query(default=50, le=200)):
    """Bedeutungen mit Beschreibung in der Ausgangssprache, die noch KEINE
    Übersetzungsverbindung zu einer Bedeutung eines Zielsprache-Wortes
    haben. Keine Obergrenze mehr — jede Bedeutung darf beliebig viele
    Übersetzungen tragen."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.id AS sense_id, m.label AS label,"
            " m.word_class AS word_class, sd.text AS text,"
            " l.id AS entry_id, w.word AS word, w.language AS language"
            " FROM meanings m"
            " JOIN lexemes l ON l.id = m.lexeme_id"
            " JOIN word_forms w ON w.id = l.id"
            " JOIN meaning_definitions sd ON sd.meaning_id = m.id"
            "  AND sd.lang = ?"
            " WHERE w.language = ?"
            " AND NOT EXISTS ("
            "  SELECT 1 FROM meaning_translations t"
            "  JOIN meanings tm ON tm.id = t.to_meaning_id"
            "  JOIN lexemes tl ON tl.id = tm.lexeme_id"
            "  JOIN word_forms tw ON tw.id = tl.id"
            "  WHERE t.from_meaning_id = m.id AND tw.language = ?)"
            " ORDER BY m.id LIMIT ?",
            (frm.lower(), frm.lower(), to.lower(), limit)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/senses/etym-queue")
def senses_etym_queue(language: str = Query(min_length=2, max_length=8),
                      limit: int = Query(default=50, le=200)):
    """Bedeutungen ohne Herkunftsbeleg — Futter für die autonome Belegung.
    Liefert Wort, Glosse und Beschreibung gleich mit."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.id AS sense_id, m.label AS label,"
            " sd.text AS text,"
            " w.word AS word, w.language AS language"
            " FROM meanings m"
            " JOIN lexemes l ON l.id = m.lexeme_id"
            " JOIN word_forms w ON w.id = l.id"
            " LEFT JOIN meaning_definitions sd ON sd.meaning_id = m.id"
            "  AND sd.lang = w.language"
            " WHERE w.language = ?"
            " AND NOT EXISTS ("
            "  SELECT 1 FROM meaning_links sl"
            "  WHERE sl.meaning_id = m.id AND sl.kind = 'etymologie')"
            " ORDER BY m.id LIMIT ?", (language.lower(), limit)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/senses/def-queue")
def senses_def_queue(frm: str = Query(min_length=2, max_length=8,
                                      alias="from"),
                     to: str = Query(min_length=2, max_length=8),
                     limit: int = Query(default=50, le=200)):
    """Bedeutungen mit Beschreibung in der Ausgangssprache, aber noch ohne
    Beschreibung in der Zielsprache — Futter für die Bedeutungs-
    Beschreibungsmatrix (jede Bedeutung in jeder Sprache erklärt)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.id AS sense_id, m.label AS label,"
            " sd.text AS text,"
            " w.word AS word, w.language AS language"
            " FROM meanings m"
            " JOIN lexemes l ON l.id = m.lexeme_id"
            " JOIN word_forms w ON w.id = l.id"
            " JOIN meaning_definitions sd ON sd.meaning_id = m.id"
            "  AND sd.lang = ?"
            " WHERE NOT EXISTS ("
            "  SELECT 1 FROM meaning_definitions zd"
            "  WHERE zd.meaning_id = m.id AND zd.lang = ?)"
            " ORDER BY m.id LIMIT ?",
            (frm.lower(), to.lower(), limit)).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.put("/senses/{sense_id}/definitions")
def put_sense_definition(sense_id: int, body: SenseDefIn,
                         _=Depends(require_api_key)):
    """Beschreibung für eine Bedeutung in einer bestimmten Sprache (Upsert)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM meanings WHERE id = ?",
                           (sense_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Sinn {sense_id} nicht gefunden")
        conn.execute(
            "INSERT OR REPLACE INTO meaning_definitions"
            " (meaning_id, lang, text, source) VALUES (?, ?, ?, ?)",
            (sense_id, body.lang.lower(), body.text.strip(), body.source))
        conn.commit()
        return {"ok": True, "sense_id": sense_id, "lang": body.lang.lower()}
    finally:
        conn.close()


def _zielbedeutung_fuer_eintrag(conn, entry_id: int) -> int:
    """Bedeutung des Ziel-Eintrags für eine Übersetzungsbeziehung.

    Genau eine vorhandene Bedeutung → diese. Keine → provisionale
    Träger-Bedeutung anlegen (mechanisch, status provisional — die
    Pipeline füllt sie aus). Mehrere → 409, die Pipeline muss die
    Bedeutung wählen (keine erfundene Zuordnung).
    """
    ms = conn.execute(
        "SELECT id FROM meanings WHERE lexeme_id = ? ORDER BY id",
        (entry_id,)).fetchall()
    if len(ms) > 1:
        raise HTTPException(
            409, f"Zieleintrag {entry_id} hat mehrere Bedeutungen —"
                 " Sinn explizit wählen")
    if len(ms) == 1:
        return ms[0]["id"]
    w = conn.execute(
        "SELECT word FROM word_forms WHERE id = ?", (entry_id,)).fetchone()
    cur = conn.execute(
        "INSERT INTO meanings (lexeme_id, label, source, status)"
        " VALUES (?, ?, 'translation-carrier', 'provisional')",
        (entry_id, (w["word"] if w else "")[:200]))
    return cur.lastrowid


@app.post("/senses/{sense_id}/translations", status_code=201)
def add_sense_translation(sense_id: int, body: SenseTransIn,
                          _=Depends(require_api_key)):
    """Übersetzungsverbindung: Bedeutung <-> Bedeutung (anderer Sprache).

    Verbindlich nach Fachmodell: keine Obergrenze der Übersetzungszahl.
    Der Zieleintrag wird auf seine (einzige/provisionale) Bedeutung
    aufgelöst; bei mehreren Bedeutungen 409 statt erfundener Zuordnung.
    """
    conn = get_conn()
    try:
        s = conn.execute("SELECT id FROM meanings WHERE id = ?",
                         (sense_id,)).fetchone()
        if not s:
            raise HTTPException(404, f"Sinn {sense_id} nicht gefunden")
        t = conn.execute("SELECT id FROM lexemes WHERE id = ?",
                         (body.entry_id,)).fetchone()
        if not t:
            raise HTTPException(404, f"Eintrag {body.entry_id} nicht gefunden")
        if body.target_sense_id is not None:
            tz = conn.execute(
                "SELECT id FROM meanings WHERE id = ? AND lexeme_id = ?",
                (body.target_sense_id, body.entry_id)).fetchone()
            if not tz:
                raise HTTPException(
                    404, f"Sinn {body.target_sense_id} gehört nicht zu"
                         f" Eintrag {body.entry_id}")
            to_mid = tz["id"]
        else:
            to_mid = _zielbedeutung_fuer_eintrag(conn, body.entry_id)
        dup = conn.execute(
            "SELECT id FROM meaning_translations"
            " WHERE from_meaning_id = ? AND to_meaning_id = ?",
            (sense_id, to_mid)).fetchone()
        if dup:
            raise HTTPException(409, "Übersetzungsverbindung existiert bereits")
        conn.execute(
            "INSERT INTO meaning_translations (from_meaning_id, to_meaning_id,"
            " approx, source) VALUES (?, ?, ?, ?)",
            (sense_id, to_mid, 1 if body.approx else 0, body.source))
        conn.commit()
        return {"ok": True, "sense_id": sense_id,
                "entry_id": body.entry_id}
    finally:
        conn.close()


@app.delete("/senses/{sense_id}/translations/{entry_id}", status_code=204)
def delete_sense_translation(sense_id: int, entry_id: int,
                             _=Depends(require_api_key)):
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM meaning_translations"
            " WHERE from_meaning_id = ? AND to_meaning_id IN"
            " (SELECT id FROM meanings WHERE lexeme_id = ?)",
            (sense_id, entry_id))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Übersetzungsverbindung nicht gefunden")


@app.post("/senses/{sense_id}/links")
def put_sense_link(sense_id: int, body: SenseLinkIn,
                   _=Depends(require_api_key)):
    """Beleg pro Bedeutung UND Aussage (claim): Wortgeschichte nachvollziehbar."""
    kind = body.kind.strip().lower()
    url = body.url.strip()
    if not url.startswith("https://"):
        raise HTTPException(400, "Nur https-URLs erlaubt")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM meanings WHERE id = ?",
                           (sense_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Sinn {sense_id} nicht gefunden")
        conn.execute(
            "INSERT OR REPLACE INTO meaning_links"
            " (meaning_id, kind, url, claim, source)"
            " VALUES (?, ?, ?, ?, ?)",
            (sense_id, kind, url, body.claim.strip(), body.source))
        conn.commit()
        return {"ok": True, "sense_id": sense_id, "kind": kind}
    finally:
        conn.close()


@app.delete("/senses/{sense_id}", status_code=204)
def delete_sense(sense_id: int, _=Depends(require_api_key)):
    """Bedeutung samt Beschreibungen, Übersetzungen und Belegen entfernen
    (Pipeline-Korrekturen, Merge falscher Dubletten)."""
    conn = get_conn()
    try:
        for tbl in ("meaning_definitions", "meaning_links",
                    "meaning_images", "meaning_examples"):
            conn.execute(f"DELETE FROM {tbl} WHERE meaning_id = ?",
                         (sense_id,))
        conn.execute(
            "DELETE FROM meaning_translations WHERE from_meaning_id = ?"
            " OR to_meaning_id = ?", (sense_id, sense_id))
        conn.execute(
            "DELETE FROM meaning_synonyms WHERE a_meaning_id = ?"
            " OR b_meaning_id = ?", (sense_id, sense_id))
        cur = conn.execute("DELETE FROM meanings WHERE id = ?", (sense_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, f"Sinn {sense_id} nicht gefunden")


@app.post("/entries/{entry_id}/source-links")
def put_source_link(entry_id: int, body: SourceLinkIn,
                    _=Depends(require_api_key)):
    """Verifizierten Externlink fuer einen Eintrag setzen (Upsert).

    Die Pipeline prueft die Existenz des Ziels VOR dem Schreiben — hier
    landen nur echt vorhandene Eintraege (keine Suchseiten, keine 404er).
    """
    kind = body.kind.strip().lower()
    url = body.url.strip()
    if not url.startswith("https://"):
        raise HTTPException(400, "Nur https-URLs erlaubt")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM lexemes WHERE id = ?",
                           (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        conn.execute(
            "INSERT OR REPLACE INTO source_links (lexeme_id, kind, url)"
            " VALUES (?, ?, ?)",
            (entry_id, kind, url))
        conn.commit()
        return {"ok": True, "entry_id": entry_id, "kind": kind}
    finally:
        conn.close()


@app.patch("/entries/{entry_id}/word", response_model=EntryOut)
def set_word(entry_id: int, body: WordIn, _=Depends(require_api_key)):
    """Wort eines Eintrags korrigieren (Tippfehler, Kodier-Müll).

    Eintrag behaelt ID, Bild, Votes und Evidenz — nur die Schreibweise
    aendert sich. Existiert das Zielwort schon, gibt es 409 (dann
    manuell zusammenfuehren, nicht ueberschreiben).
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT language FROM word_forms WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        dup = conn.execute(
            "SELECT 1 FROM word_forms WHERE word = ? AND language = ?"
            " AND id != ?",
            (body.word.strip(), row["language"], entry_id),
        ).fetchone()
        if dup:
            raise HTTPException(
                409, f"'{body.word.strip()}' existiert bereits "
                     f"({row['language']}) — Zusammenfuehrung noetig")
        conn.execute(
            "UPDATE word_forms SET word = ? WHERE id = ?",
            (body.word.strip(), entry_id),
        )
        conn.commit()
        r2 = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.id = ?", (entry_id,)).fetchone()
        return _entry_from_row(conn, r2)
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise HTTPException(409, f"'{body.word.strip()}' existiert bereits")
    finally:
        conn.close()


# ---------- Wortdaten-Bundle (generische Wortseite) ----------

def _bundle_fuer_lexem(conn, form_row, lex_row) -> dict:
    """Ein Lesart-Eintrag des Bundles: Lexem mit Bedeutungen, Bildern,
    Grammatik, Aussprachen und Stimmbilanzen — generisch, keine
    Sonderbehandlung einzelner Wörter."""
    lex_id = lex_row["id"]
    attrs = _attrs(lex_row)
    definitions: dict[str, dict] = {}
    for d in conn.execute(
            "SELECT lang, text, source FROM lexeme_defs"
            " WHERE lexeme_id = ? ORDER BY lang", (lex_id,)).fetchall():
        definitions[d["lang"]] = {"text": d["text"], "source": d["source"]}
    imgs = conn.execute(
        "SELECT filename, is_main FROM lexeme_images"
        " WHERE lexeme_id = ? ORDER BY id", (lex_id,)).fetchall()
    examples = [r["text"] for r in conn.execute(
        "SELECT text FROM lexeme_examples WHERE lexeme_id = ? ORDER BY id",
        (lex_id,)).fetchall()]
    links = [{"kind": l["kind"], "url": l["url"]} for l in conn.execute(
        "SELECT kind, url FROM source_links WHERE lexeme_id = ? ORDER BY kind",
        (lex_id,)).fetchall()]
    # Grammatik: Wortart/Merkmale dieses Lexems + Paradigma der Familie
    # (lemma-gemittelt, wie /grammatik).
    g = conn.execute(
        f"{_ENTRY_SELECT} WHERE w.language = ? AND l.lemma = ?"
        " ORDER BY w.id LIMIT 1",
        (lex_row["language"], lex_row["lemma"] or form_row["word"]),
    ).fetchone()
    grammar = _grammar_for_lexeme(conn, g) if g else None
    # Lesart (genau diese Form+Lexem-Kombination) mit Aussprache und Audio
    reading = conn.execute(
        "SELECT id FROM readings WHERE form_id = ? AND lexeme_id = ?",
        (form_row["id"], lex_id)).fetchone()
    readings = []
    if reading:
        rid = reading["id"]
        prons = [{"transcription": p["transcription"], "note": p["note"]}
                 for p in conn.execute(
                     "SELECT transcription, note FROM pronunciations"
                     " WHERE reading_id = ? ORDER BY id", (rid,)).fetchall()]
        audio = [{"voice": a["voice"] or None, "length_scale": a["length_scale"],
                  "filename": a["filename"]}
                 for a in conn.execute(
                     "SELECT voice, length_scale, filename FROM reading_audio"
                     " WHERE reading_id = ? ORDER BY voice, length_scale",
                     (rid,)).fetchall()]
        readings.append({"id": rid, "pronunciations": prons, "audio": audio})
    # Stimmbilanzen
    img = conn.execute(
        "SELECT SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja,"
        " SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein"
        " FROM ratings WHERE lexeme_id = ? AND kind = 'image'",
        (lex_id,)).fetchone()
    unknown = conn.execute(
        "SELECT COUNT(*) n FROM ratings WHERE lexeme_id = ? AND kind = 'unknown'",
        (lex_id,)).fetchone()["n"]
    wr = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM lexeme_ratings WHERE lexeme_id = ?"
        " GROUP BY kind", (lex_id,)).fetchall()}
    per_img = {r["image"]: {"ja": r["ja"] or 0, "nein": r["nein"] or 0}
               for r in conn.execute(
        "SELECT image,"
        " SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja,"
        " SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein"
        " FROM ratings WHERE lexeme_id = ? AND kind = 'image'"
        " GROUP BY image", (lex_id,)).fetchall()}
    meaning_list = []
    for m in _senses_fuer_eintrag(conn, lex_id):
        meaning_list.append(m)
    evidence = lex_row["evidence"]
    try:
        evidence = json.loads(evidence) if isinstance(evidence, str) else evidence
    except Exception:
        evidence = None
    return {
        "id": lex_id,
        "lemma": lex_row["lemma"],
        "word_class": lex_row["word_class"],
        "attrs": attrs,
        "status": lex_row["status"],
        "source": lex_row["source"],
        "created_at": lex_row["created_at"],
        "confidence": lex_row["confidence"],
        "sprache_unsicher": lex_row["sprache_unsicher"],
        "evidence": evidence,
        "definitions": definitions,
        "images": {"main": next((i["filename"] for i in imgs if i["is_main"]),
                                None),
                   "gallery": [i["filename"] for i in imgs if not i["is_main"]]},
        "examples": examples,
        "grammar": grammar,
        "paradigm": [{"cell": json.loads(lf["cell"] or "[]"), "word": lf["word"]}
                     for lf in conn.execute(
            "SELECT lf.cell, w.word FROM lexeme_forms lf"
            " JOIN word_forms w ON w.id = lf.form_id"
            " WHERE lf.lexeme_id = ? ORDER BY lf.id", (lex_id,)).fetchall()],
        "meanings": meaning_list,
        "readings": readings,
        "source_links": links,
        "ratings": {
            "image": {"ja": img["ja"] or 0, "nein": img["nein"] or 0},
            "unknown": unknown,
            "word": {"dazu": wr.get("dazu", 0), "nicht": wr.get("nicht", 0),
                     "unsicher": wr.get("unsicher", 0)},
            "per_image": per_img,
        },
    }


@app.get("/wortdaten")
def wortdaten(word: str, language: str):
    """Generisches Wortdaten-Bundle für die Wortseite.

    Eine Schreibweise (Wortform), darunter alle Lesarten (Lexeme) mit
    ihren Bedeutungen, Bildern, Grammatiken und Aussprachen. Die
    Darstellung entscheidet nichts selbst — sie zeigt, was das Modell
    hergibt (inkl. unzugeordnetem Wortebenen-Bestand).
    """
    conn = get_conn()
    try:
        form = conn.execute(
            "SELECT id, word, language, script FROM word_forms"
            " WHERE word = ? AND language = ?",
            (word.strip(), language.lower())).fetchone()
        if not form:
            raise HTTPException(404, f"'{word}' nicht gefunden ({language})")
        # Lesarten = (form, lexeme)-Paare; Fallback: das 1:1-Lexem der Form
        lesarten = conn.execute(
            f"{_ENTRY_SELECT} JOIN readings r"
            " ON r.form_id = w.id AND r.lexeme_id = l.id"
            " WHERE w.id = ? AND l.status <> 'candidate' ORDER BY l.id",
            (form["id"],)).fetchall()
        if not lesarten:
            lex = conn.execute(
                f"{_ENTRY_SELECT} WHERE w.id = ?"
                " AND l.status <> 'candidate'", (form["id"],)).fetchone()
            lesarten = [lex] if lex else []
        lexemes = [_bundle_fuer_lexem(conn, form, lr) for lr in lesarten]
        return {
            "form": {"id": form["id"], "word": form["word"],
                     "language": form["language"], "script": form["script"]},
            "lexemes": lexemes,
        }
    finally:
        conn.close()


# ---------- Bilder ----------

@app.get("/bilder/neu", include_in_schema=False)
def bilder_neu(limit: int = Query(default=12, le=30)):
    """Die zuletzt verknüpften Bilder (Galerie-Zeitstrahl) mit Wort-Link."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT li.filename AS filename, w.word AS word, "
            "       w.language AS language, li.lexeme_id AS entry_id "
            "FROM lexeme_images li "
            "JOIN word_forms w ON w.id = li.lexeme_id "
            "ORDER BY li.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/bilder/{filename}", include_in_schema=False)
def bilder(filename: str):
    """Generierte Wort-Bilder ausliefern (nur simple Dateinamen)."""
    if not IMAGE_NAME_RE.fullmatch(filename):
        raise HTTPException(404, "Ungültiger Dateiname")
    path = IMAGES_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "Bild nicht gefunden")
    return FileResponse(path)


# ---------- Anonyme Bewertungssitzungen (Cookie + Nonce, keine IP) ----------

SESSION_COOKIE = "wb_session"
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SESSION_IDLE_H = 12          # Sitzung verfaellt nach 12 h Inaktivitaet
NONCE_TTL_MIN = 30           # Einmal-Nonce pro gezogenem Wort
SESSION_VOTE_PER_MIN = 30    # grosszuegiges Tempolimit pro Sitzung

_session_votes: dict[str, deque] = defaultdict(deque)
_session_votes_lock = threading.Lock()


def _session_vote_throttle(sid: str) -> None:
    """Max SESSION_VOTE_PER_MIN Stimmen pro Sitzung und Minute."""
    now = time.monotonic()
    with _session_votes_lock:
        dq = _session_votes[sid]
        while dq and dq[0] <= now - WINDOW:
            dq.popleft()
        if len(dq) >= SESSION_VOTE_PER_MIN:
            raise HTTPException(429, "Zu schnell — kurz pausieren.")
        dq.append(now)


def _get_session(request: Request, response: Response) -> str:
    """Vorhandene gueltige Sitzung uebernehmen oder neue anlegen.

    Nur eine zufaellige Kennung im HttpOnly-Cookie — es wird kein
    Merkmal gespeichert, das einen Nutzer dauerhaft identifiziert.
    """
    sid = request.cookies.get(SESSION_COOKIE) or ""
    conn = get_conn()
    try:
        row = None
        if SESSION_RE.fullmatch(sid):
            row = conn.execute(
                "SELECT id FROM rating_sessions WHERE id = ? AND "
                "last_seen > ?", (sid, now_str(-SESSION_IDLE_H * 60)),
            ).fetchone()
        if row:
            conn.execute("UPDATE rating_sessions SET last_seen = datetime('now') "
                         "WHERE id = ?", (sid,))
            conn.commit()
            return sid
        sid = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO rating_sessions (id) VALUES (?)", (sid,))
        conn.commit()
    finally:
        conn.close()
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=SESSION_IDLE_H * 3600, httponly=True,
        samesite="lax", secure=request.url.scheme == "https")
    return sid


@app.get("/raten/next")
def raten_next(request: Request, response: Response,
               language: str | None = None, exclude: str = ""):
    """Bild für die Bewertungsseite: wenig bewertete zuerst, dann Zufall.

    Legt anonyme Sitzung an (Cookie) und liefert einen Einmal-Nonce,
    der zum Abstimmen noetig ist.
    """
    sql = ("SELECT l.id, w.word, w.language,"
           " (SELECT filename FROM lexeme_images li WHERE li.lexeme_id = l.id"
           "  AND li.is_main = 1) AS image,"
           " (SELECT COUNT(*) FROM ratings r WHERE r.lexeme_id = l.id "
           " AND r.kind = 'image') AS votes "
           "FROM lexemes l JOIN word_forms w ON w.id = l.id "
           "WHERE EXISTS (SELECT 1 FROM lexeme_images li"
           " WHERE li.lexeme_id = l.id AND li.is_main = 1)")
    params: list = []
    if language:
        sql += " AND w.language = ?"
        params.append(language.lower())
    seen = [int(x) for x in exclude.split(",") if x.strip().isdigit()][:300]
    if seen:
        sql += f" AND l.id NOT IN ({','.join('?' * len(seen))})"
        params += seen
    sql += " ORDER BY votes ASC, RANDOM() LIMIT 1"
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        if not row:
            raise HTTPException(404, "Keine neuen Bilder mehr vorhanden")
        out = row_to_dict(row)
        out["definition"] = _de_definition(conn, out["id"])
        # Sitzung + Einmal-Nonce (an Sitzung und Eintrag gebunden)
        sid = _get_session(request, response)
        nonce = secrets.token_urlsafe(16)
        conn.execute(
            "DELETE FROM rating_nonces WHERE expires_at < datetime('now')")
        conn.execute(
            "INSERT INTO rating_nonces (nonce, session_id, lexeme_id, "
            "expires_at) VALUES (?, ?, ?, ?)",
            (nonce, sid, out["id"], now_str(NONCE_TTL_MIN)))
        conn.commit()
        out["nonce"] = nonce
        # Galerie: weitere Bilder, die den Begriff mitbeschreiben
        out["gallery"] = [r["filename"] for r in conn.execute(
            "SELECT filename FROM lexeme_images "
            "WHERE lexeme_id = ? AND filename != ? AND is_main = 0"
            " ORDER BY id",
            (out["id"], out["image"] or "")).fetchall()]
    finally:
        conn.close()
    # Alternative Kandidaten desselben Wortes (rendern liefert immer
    # mehrere Entwürfe; Gewinner steht als is_main) — direkt vom
    # Dateisystem, keine DB-Spalte noetig.
    out["candidates"] = []
    if IMAGE_NAME_RE.fullmatch(out["image"] or ""):
        stem = out["image"].rsplit(".", 1)[0]
        for n in range(1, 6):
            cand = f"{stem}-cand{n}.png"
            if (IMAGES_DIR / cand).is_file() and cand != out["image"] \
                    and cand not in out["gallery"]:
                out["candidates"].append(cand)
    return out


@app.get("/zufall")
def zufall(language: str = ""):
    """Zufälliger Eintrag mit Bild — Entdecken-Karte auf der Startseite.
    Leere Sprache = beliebige Sprache."""
    conn = get_conn()
    try:
        base = ("SELECT w.word, w.language,"
                " (SELECT filename FROM lexeme_images li"
                "  WHERE li.lexeme_id = w.id AND li.is_main = 1) AS image"
                " FROM word_forms w"
                " WHERE EXISTS (SELECT 1 FROM lexeme_images li"
                " WHERE li.lexeme_id = w.id AND li.is_main = 1)")
        if language:
            row = conn.execute(
                base + " AND w.language = ? ORDER BY RANDOM() LIMIT 1",
                (language.lower(),)).fetchone()
        else:
            row = conn.execute(
                base + " ORDER BY RANDOM() LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Keine Einträge mit Bild in dieser Sprache")
    out = row_to_dict(row)
    conn2 = get_conn()
    try:
        lex_id = _lexem_id_zu_form(conn2, out["word"], out["language"])
        out["id"] = lex_id
        out["definition"] = _de_definition(conn2, lex_id) if lex_id else None
    finally:
        conn2.close()
    return out


def _lexem_id_zu_form(conn, word: str, language: str) -> int | None:
    r = conn.execute(
        "SELECT id FROM word_forms WHERE word = ? AND language = ?",
        (word, language)).fetchone()
    return r["id"] if r else None


@app.post("/raten/pick")
def raten_pick(pick: PickIn):
    """Kandidaten-Bild zum offiziellen Bild machen (Voting-Eingriff).

    Erlaubt nur Dateien, die zum Eintrag gehoeren: <lang>-<id>-cand<N>.png
    oder <lang>-<id>-v<N>.png, und die auf der Platte existieren.
    Das bisherige Hauptbild bleibt als Galerie-Bild erhalten — die
    Kombination mehrerer Bilder beschreibt den Begriff. Stimmen werden
    pro Datei gezaehlt: alte Hauptbild-Stimmen (image = '') werden auf
    den Dateinamen des alten Hauptbilds umgebogen.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT w.language, l.id AS lex_id FROM lexemes l"
            " JOIN word_forms w ON w.id = l.id WHERE l.id = ?",
            (pick.entry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {pick.entry_id} nicht gefunden")
        prefix = f"{row['language']}-{pick.entry_id}-"
        if not pick.filename.startswith(prefix):
            raise HTTPException(400, "Datei gehört nicht zu diesem Eintrag")
        if not (IMAGES_DIR / pick.filename).is_file():
            raise HTTPException(404, "Bilddatei nicht vorhanden")
        old = _main_image(conn, pick.entry_id) or ""
        if old != pick.filename:
            if old:
                conn.execute(
                    "INSERT OR IGNORE INTO lexeme_images (lexeme_id, filename)"
                    " VALUES (?, ?)", (pick.entry_id, old))
                conn.execute(
                    "UPDATE ratings SET image = ? WHERE lexeme_id = ? "
                    "AND image = ''", (old, pick.entry_id))
            conn.execute(
                "INSERT OR IGNORE INTO lexeme_images (lexeme_id, filename)"
                " VALUES (?, ?)", (pick.entry_id, pick.filename))
            _set_main_image(conn, pick.entry_id, pick.filename)
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "image": pick.filename}


@app.post("/bilder/upload", status_code=201)
def bilder_upload(body: ImageIn, _=Depends(require_api_key)):
    """Bilddatei nur speichern (ohne Hauptbild zu ändern) — für
    Kandidaten-Entwürfe der Bild-Pipeline."""
    try:
        data = base64.b64decode(body.data_b64, validate=True)
    except Exception:
        raise HTTPException(400, "Ungültiges Base64")
    if len(data) > 2_000_000:
        raise HTTPException(413, "Bild zu groß (max. 2 MB)")
    path = IMAGES_DIR / body.filename
    if path.is_file() and path.stat().st_size == len(data):
        return {"ok": True, "filename": body.filename, "cached": True}
    path.write_bytes(data)
    return {"ok": True, "filename": body.filename}


@app.post("/raten/vote", status_code=201)
def raten_vote(vote: VoteIn, request: Request):
    """Öffentliche Bewertung (kein Key nötig): 'Passt'/'Passt nicht' oder
    'Wort unbekannt' — letzteres zaehlt nicht gegen das Bild.

    Oeffentliche Stimmen brauchen Sitzungs-Cookie + Einmal-Nonce aus
    /raten/next: Der Nonce wird atomar verbraucht, pro Sitzung und Bild
    zaehlt nur die erste Stimme. Mit API-Key (Pipeline) entfaellt das —
    dann zaehlt weiter jede Stimme.
    """
    keyed = bool(API_KEY) and request.headers.get("x-api-key") == API_KEY
    sid = request.cookies.get(SESSION_COOKIE) or ""
    if not keyed:
        if not SESSION_RE.fullmatch(sid):
            raise HTTPException(403, "Sitzung fehlt — Seite neu laden.")
        _session_vote_throttle(sid)
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM lexemes WHERE id = ?", (vote.entry_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Eintrag {vote.entry_id} nicht gefunden")
        image = vote.image or ""
        if not keyed:
            # Nonce atomar verbrauchen (gebunden an Sitzung + Eintrag)
            cur = conn.execute(
                "DELETE FROM rating_nonces WHERE nonce = ? AND session_id = ? "
                "AND lexeme_id = ? AND expires_at > datetime('now')",
                (vote.nonce or "", sid, vote.entry_id))
            if cur.rowcount != 1:
                raise HTTPException(403, "Ungültiger oder verbrauchter Nonce.")
            # Dasselbe Bild in derselben Sitzung nur einmal
            dup = conn.execute(
                "SELECT 1 FROM ratings WHERE session_id = ? AND lexeme_id = ? "
                "AND image = ?",
                (sid, vote.entry_id, image)).fetchone()
            if dup:
                raise HTTPException(409, "Bereits bewertet.")
        conn.execute(
            "INSERT INTO ratings (lexeme_id, understood, image, kind, "
            "session_id) VALUES (?, ?, ?, ?, ?)",
            (vote.entry_id, 1 if vote.understood else 0, image,
             vote.kind, sid or None),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------- Bild-Umzug: "passt besser zu einem anderen Wort" ----------

@app.post("/raten/move", status_code=201)
def raten_move(move: MoveIn):
    """Öffentlicher Umzugswunsch. Die Pipeline prueft ihn und fuehrt ihn
    ggf. aus (letzte Entscheidung durchs Modell, nicht durch den Button)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT l.id, w.language FROM lexemes l"
            " JOIN word_forms w ON w.id = l.id WHERE l.id = ?",
            (move.entry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {move.entry_id} nicht gefunden")
        mine = {r["filename"] for r in conn.execute(
            "SELECT filename FROM lexeme_images WHERE lexeme_id = ?",
            (move.entry_id,)).fetchall()}
        if move.filename not in mine:
            raise HTTPException(400,
                                "Datei gehört nicht zu diesem Eintrag")
        target = conn.execute(
            "SELECT id FROM word_forms WHERE word = ? AND language = ?",
            (move.target_word.strip(), row["language"]),
        ).fetchone()
        if not target:
            raise HTTPException(404,
                                f"'{move.target_word}' gibt es im "
                                f"{row['language']}-Bestand nicht")
        conn.execute(
            "INSERT INTO image_moves (filename, from_lexeme_id, target_word,"
            " language) VALUES (?, ?, ?, ?)",
            (move.filename, move.entry_id,
             move.target_word.strip(), row["language"]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/raten/moves")
def raten_moves(status: str = "offen", _=Depends(require_api_key)):
    """Offene/abgeschlossene Umzugswuensche (für die Pruef-Pipeline)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT m.*, w.word AS from_word FROM image_moves m "
            "JOIN word_forms w ON w.id = m.from_lexeme_id "
            "WHERE m.status = ? ORDER BY m.id",
            (status,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/raten/moves/{move_id}/resolve")
def raten_resolve(move_id: int, body: ResolveIn,
                  _=Depends(require_api_key)):
    """Umzug ausfuehren (accept) oder ablehnen — nach Modellprüfung."""
    conn = get_conn()
    try:
        m = conn.execute(
            "SELECT * FROM image_moves WHERE id = ?", (move_id,),
        ).fetchone()
        if not m:
            raise HTTPException(404, f"Umzug {move_id} nicht gefunden")
        if m["status"] != "offen":
            raise HTTPException(409, "Umzug ist schon entschieden")
        note = body.note
        if body.accept:
            src_main = conn.execute(
                "SELECT 1 FROM lexeme_images WHERE lexeme_id = ?"
                " AND filename = ? AND is_main = 1",
                (m["from_lexeme_id"], m["filename"])).fetchone()
            is_main = bool(src_main)
            if is_main:
                nxt = conn.execute(
                    "SELECT filename FROM lexeme_images "
                    "WHERE lexeme_id = ? AND filename != ?"
                    " ORDER BY id LIMIT 1",
                    (m["from_lexeme_id"], m["filename"])).fetchone()
                conn.execute(
                    "UPDATE lexeme_images SET is_main = 0"
                    " WHERE lexeme_id = ?", (m["from_lexeme_id"],))
                if nxt:
                    conn.execute(
                        "UPDATE lexeme_images SET is_main = 1"
                        " WHERE lexeme_id = ? AND filename = ?",
                        (m["from_lexeme_id"], nxt["filename"]))
            conn.execute(
                "DELETE FROM lexeme_images WHERE lexeme_id = ?"
                " AND filename = ?",
                (m["from_lexeme_id"], m["filename"]))
            # Stimmen im alten Kontext verwirren nur
            conn.execute(
                "DELETE FROM ratings WHERE lexeme_id = ? AND image = ?",
                (m["from_lexeme_id"], m["filename"]))
            tgt = conn.execute(
                "SELECT l.id FROM lexemes l JOIN word_forms w ON w.id = l.id"
                " WHERE w.word = ? AND w.language = ?",
                (m["target_word"], m["language"])).fetchone()
            if not tgt:
                raise HTTPException(404, "Zieleintrag nicht gefunden")
            conn.execute(
                "INSERT OR IGNORE INTO lexeme_images (lexeme_id, filename)"
                " VALUES (?, ?)", (tgt["id"], m["filename"]))
            if not _main_image(conn, tgt["id"]):
                conn.execute(
                    "UPDATE lexeme_images SET is_main = 1"
                    " WHERE lexeme_id = ? AND filename = ?",
                    (tgt["id"], m["filename"]))
        conn.execute(
            "UPDATE image_moves SET status = ?, note = ?, "
            "decided_at = datetime('now') WHERE id = ?",
            ("angenommen" if body.accept else "abgelehnt", note, move_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "accepted": body.accept}


@app.get("/raten/schlecht")
def raten_schlecht(min_votes: int = 2, _=Depends(require_api_key)):
    """Einträge, deren Bilder mehrheitlich 'nicht verstanden' votiert wurden
    (für die Nachbesserungs-Pipeline)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT l.id, w.word, w.language,"
            " (SELECT text FROM lexeme_defs d WHERE d.lexeme_id = l.id"
            "  AND d.lang = 'de') AS definition,"
            " (SELECT filename FROM lexeme_images li WHERE li.lexeme_id = l.id"
            "  AND li.is_main = 1) AS image, "
            "SUM(CASE WHEN r.understood = 1 THEN 1 ELSE 0 END) AS ja, "
            "SUM(CASE WHEN r.understood = 0 THEN 1 ELSE 0 END) AS nein "
            "FROM ratings r JOIN lexemes l ON l.id = r.lexeme_id "
            "JOIN word_forms w ON w.id = l.id "
            "WHERE r.kind = 'image' "
            "GROUP BY r.lexeme_id "
            "HAVING nein > ja AND nein >= ? "
            "ORDER BY nein DESC",
            (min_votes,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/raten/stats")
def raten_stats():
    conn = get_conn()
    try:
        votes = conn.execute(
            "SELECT COUNT(*) AS n FROM ratings WHERE kind = 'image'"
        ).fetchone()["n"]
        understood = conn.execute(
            "SELECT COUNT(*) AS n FROM ratings WHERE understood = 1 "
            "AND kind = 'image'"
        ).fetchone()["n"]
        unknown = conn.execute(
            "SELECT COUNT(*) AS n FROM ratings WHERE kind = 'unknown'"
        ).fetchone()["n"]
        with_image = conn.execute(
            "SELECT COUNT(DISTINCT lexeme_id) AS n FROM lexeme_images"
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"votes": votes, "understood": understood,
            "not_understood": votes - understood, "unknown": unknown,
            "entries_with_image": with_image}


# ---------- Wort-Konfidenz & Evidenz ----------

class BildVoteIn(BaseModel):
    entry_id: int
    understood: bool
    # konkrete Bilddatei (Galerie), leer/fehlend = Hauptbild
    image: str | None = Field(default=None, max_length=150)


@app.post("/wort/bildvote", status_code=201)
def wort_bildvote(vote: BildVoteIn, request: Request, response: Response):
    """Oeffentliche Bild-Stimme direkt von der Wortseite.

    Wie die Kuratier-Stimme ohne Nonce, aber eine Stimme pro Sitzung
    und Bild (Cookie-Kennung, kein Merkmal) — gegen Doppelklicks und
    leichtes Fluten.
    """
    sid = _get_session(request, response)
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM lexemes WHERE id = ?", (vote.entry_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Eintrag {vote.entry_id} nicht gefunden")
        img = vote.image or ""
        dup = conn.execute(
            "SELECT 1 FROM ratings WHERE lexeme_id = ? AND session_id = ? "
            "AND kind = 'image' AND image = ?",
            (vote.entry_id, sid, img),
        ).fetchone()
        if dup:
            raise HTTPException(409, "Diese Sitzung hat hier schon abgestimmt")
        conn.execute(
            "INSERT INTO ratings (lexeme_id, understood, kind, image,"
            " session_id) VALUES (?, ?, 'image', ?, ?)",
            (vote.entry_id, 1 if vote.understood else 0, img, sid),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/wort/vote", status_code=201)
def wort_vote(vote: WordVoteIn):
    """Öffentliche Kuratier-Stimme: Gehört das Wort zur Sprache?

    Drei Faelle: 'dazu' (Gebrauchswort), 'nicht' (falsche Sprache),
    'unsicher'. Unabhängig vom Bild-Voting — ein Wort kann ein gutes
    Bild haben und trotzdem fehl am Platz sein (und umgekehrt).
    """
    kind = vote.kind
    if kind is None:
        kind = "dazu" if vote.fits else "nicht"
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM lexemes WHERE id = ?", (vote.entry_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Eintrag {vote.entry_id} nicht gefunden")
        conn.execute(
            "INSERT INTO lexeme_ratings (lexeme_id, kind) VALUES (?, ?)",
            (vote.entry_id, kind),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/entries/{entry_id}/evidence")
def get_evidence(entry_id: int):
    """Alles, was zur Konfidenz eines Wortes beiträgt."""
    conn = get_conn()
    try:
        row = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.id = ?", (entry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        img = conn.execute(
            "SELECT SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja,"
            "       SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein "
            "FROM ratings WHERE lexeme_id = ? AND kind = 'image'", (entry_id,)
        ).fetchone()
        wr_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM lexeme_ratings "
            "WHERE lexeme_id = ? GROUP BY kind", (entry_id,)
        ).fetchall()
        wr_counts = {r["kind"]: r["n"] for r in wr_rows}
        unbekannt = conn.execute(
            "SELECT COUNT(*) AS n FROM ratings "
            "WHERE lexeme_id = ? AND kind = 'unknown'", (entry_id,)
        ).fetchone()["n"]
        # Stimmen pro Bilddatei ('' = Hauptbild)
        per_img = {}
        for r in conn.execute(
                "SELECT image AS img,"
                " SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja,"
                " SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein "
                "FROM ratings WHERE lexeme_id = ? AND kind = 'image' "
                "GROUP BY image",
                (entry_id,)).fetchall():
            per_img[r["img"]] = {"ja": r["ja"] or 0, "nein": r["nein"] or 0}
        main = _main_image(conn, entry_id)
        gallery = [g["filename"] for g in conn.execute(
            "SELECT filename FROM lexeme_images WHERE lexeme_id = ? "
            "AND filename != ? ORDER BY id",
            (entry_id, main or "")).fetchall()]
        # Grammatik des Lemmas (Wortart, Genus, Flexion) — einmal pro
        # Wortfamilie, die Einzelworte verweisen darauf.
        g = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.language = ? AND l.lemma = ?"
            " ORDER BY w.id LIMIT 1",
            (row["language"], row["lemma"] or row["word"]),
        ).fetchone()
        links = [{"kind": l["kind"], "url": l["url"]} for l in conn.execute(
            "SELECT kind, url FROM source_links WHERE lexeme_id = ? "
            "ORDER BY kind", (entry_id,)).fetchall()]
        out = _entry_from_row(conn, row, with_evidence=False)
        out["evidence"] = None
        try:
            out["evidence"] = json.loads(row["evidence"] or "{}")
        except Exception:
            out["evidence"] = {}
        out["image_votes"] = {"ja": img["ja"] or 0, "nein": img["nein"] or 0}
        out["word_votes"] = {"dazu": wr_counts.get("dazu", 0),
                             "nicht": wr_counts.get("nicht", 0),
                             "unsicher": wr_counts.get("unsicher", 0)}
        # "Wort unbekannt"-Stimmen der Bewertungsseite — eigenes Signal:
        # verstehen Menschen den Begriff, unabhaengig vom Bild?
        out["unknown_votes"] = unbekannt
        # Grammatik der Wortfamilie (NULL, wenn das Lemma noch nicht
        # erntet wurde)
        out["grammar"] = _grammar_for_lexeme(conn, g) if g else None
        # Bilder des Eintrags: Hauptbild zuerst, dann Galerie — jeweils mit
        # ihren eigenen Stimmen (Kombination beschreibt den Begriff).
        bilder = []
        if main:
            v = per_img.get(main, per_img.get("", {"ja": 0, "nein": 0}))
            bilder.append({"filename": main, "main": True, **v})
        for fn in gallery:
            v = per_img.get(fn, {"ja": 0, "nein": 0})
            bilder.append({"filename": fn, "main": False, **v})
        out["bilder"] = bilder
        out["source_links"] = links
        return out
    finally:
        conn.close()


@app.post("/entries/{entry_id}/evidence", response_model=EntryOut)
def set_evidence(entry_id: int, body: EvidenceIn,
                 _=Depends(require_api_key)):
    """Konfidenz + Evidenz speichern (Pruef-Pipeline)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE lexemes SET confidence = ?, evidence = ?,"
            " sprache_unsicher = ? WHERE id = ?",
            (body.confidence, json.dumps(body.evidence, ensure_ascii=False),
             1 if body.sprache_unsicher else 0, entry_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        row = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.id = ?", (entry_id,)
        ).fetchone()
        return _entry_from_row(conn, row)
    finally:
        conn.close()


# ---------- Anhören (Piper TTS) ----------

def _trim_wav(src: Path, dst: Path) -> None:
    """Stille am Anfang/Ende kappen — mit Pre-Roll, damit weiche Anlaute
    (z. B. «qu», «s») nicht abgehackt wirken.

    Reines Python (wave-Modul), kein ffmpeg nötig. Begründung: ffmpeg
    silenceremove (-45 dB) hat den leisen Konsonanten-Anlaut mitgeschnitten
    und das Wort abrupt auf Volllautstärke starten lassen (analysiert am
    23.11. an Thorsten/«Quallenverseuchung»).
    """
    try:
        import wave
        with wave.open(str(src), "rb") as w:
            sr, sw, ch = (w.getframerate(), w.getsampwidth(),
                          w.getnchannels())
            if sw != 2 or ch != 1:
                raise ValueError("unerwartetes WAV-Format")
            n = w.getnframes()
            samples = struct.unpack(f"<{n}h", w.readframes(n))
        peak = max((abs(s) for s in samples), default=0)
        if peak == 0:
            raise ValueError("leere Synthese")
        # Laut-Abfrage: Mittel-Amplitude > 1 % des Peaks (≈ -40 dB) im
        # 10-ms-Fenster — weitaus sanfter als der alte -45-dB-Peak-Filter.
        grenze = max(500, int(peak * 0.01))
        win = max(1, sr // 100)

        def laut(i: int) -> bool:
            seg = samples[i:i + win]
            return sum(abs(s) for s in seg) > grenze * len(seg)

        start = next((i for i in range(0, max(n - win, 1), win) if laut(i)), 0)
        ende = next((i for i in range(n, win - 1, -win) if laut(i - win)), n)
        start = max(0, start - int(sr * 0.06))   # 60 ms Anlaut behalten
        ende = min(n, ende + int(sr * 0.12))     # 120 ms Ausklingen lassen
        with wave.open(str(dst), "wb") as o:
            o.setnchannels(1)
            o.setsampwidth(2)
            o.setframerate(sr)
            o.writeframes(struct.pack(f"<{ende - start}h",
                                      *samples[start:ende]))
    except Exception:
        shutil.copyfile(src, dst)


def _synth_wort(text: str, lang: str, voice_key: str, out: Path,
                length_scale: float | None = None) -> None:
    """Piper-Synthese für ein Wort, getrimmt nach out schreiben."""
    voice = PIPER_VOICES[lang][voice_key]
    model = PIPER_DIR / "voices" / voice["model"]
    piper = PIPER_DIR / "piper" / "piper"
    if not piper.is_file() or not model.is_file():
        raise HTTPException(503, "Sprachausgabe auf diesem Server "
                                 "nicht eingerichtet")
    ls = length_scale if length_scale is not None else voice["length_scale"]
    tmp = out.with_suffix(".tmp.wav")
    try:
        proc = subprocess.run(
            [str(piper), "--model", str(model), "--length-scale", str(ls),
             "--output_file", str(tmp)],
            input=(text + "\n").encode(),
            capture_output=True, timeout=120)
        if proc.returncode != 0 or not tmp.is_file():
            raise HTTPException(500,
                                "Synthese fehlgeschlagen: "
                                + proc.stderr.decode(errors="replace")
                                [:300])
        _trim_wav(tmp, out)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Synthesefehler: {e}")
    finally:
        if tmp.is_file():
            tmp.unlink()


def _default_voice(lang: str) -> str:
    """Erste Stimme einer Sprache = Default."""
    return next(iter(PIPER_VOICES[lang]))


def _stimme_namen(lang: str, voice: str | None) -> str:
    if voice is None:
        return _default_voice(lang)
    if voice not in PIPER_VOICES[lang]:
        raise HTTPException(404, f"Sprecher '{voice}' nicht bekannt ({lang})")
    return voice


def _lesart_fuer(conn, form_id: int):
    """Lesart (reading) einer Wortform — 1:1-Fall: (form, form)."""
    return conn.execute(
        "SELECT id FROM readings WHERE form_id = ? ORDER BY id LIMIT 1",
        (form_id,)).fetchone()


def _audio_als_db_zeile(conn, form_id: int, voice: str, ls: float,
                        default_voice: str):
    """Passende Audiodatei aus reading_audio (inkl. stimmenlosem
    Altbestand, voice='' = Default-Stimme)."""
    row = conn.execute(
        "SELECT a.filename FROM reading_audio a"
        " JOIN readings r ON r.id = a.reading_id"
        " WHERE r.form_id = ? AND (a.voice = ? OR (a.voice = '' AND ?))"
        " AND a.length_scale = ?"
        " ORDER BY (a.voice = '') LIMIT 1",
        (form_id, voice, 1 if voice == default_voice else 0, ls)).fetchone()
    return row["filename"] if row else None


def _audio_dbschreiben(conn, form_id: int, lexeme_id: int | None,
                       voice: str, ls: float, filename: str) -> None:
    """Synthese-Ergebnis als reading_audio-Zeile festhalten."""
    r = _lesart_fuer(conn, form_id)
    if not r:
        conn.execute(
            "INSERT OR IGNORE INTO readings (form_id, lexeme_id, source)"
            " VALUES (?, ?, 'synthesis')", (form_id, lexeme_id))
        r = _lesart_fuer(conn, form_id)
    conn.execute(
        "INSERT OR IGNORE INTO reading_audio (reading_id, voice,"
        " length_scale, filename) VALUES (?, ?, ?, ?)",
        (r["id"], voice, ls, filename))


@app.get("/anhoeren")
def anhoeren(word: str, language: str = "de", voice: str | None = None,
             length_scale: float | None = None):
    """Wort aussprechen (Piper auf dem Pi). Gecacht pro Eintrag und
    Sprecher: audio/<lang>-<id>-<voice>.wav — Synthese nur beim ersten
    Aufruf. Unbekannte Wörter laufen als Test (eigener Cache-Namespace),
    damit die Sprecher-Testseite freie Eingaben erlaubt."""
    lang = language.lower()
    if lang not in PIPER_VOICES:
        raise HTTPException(404, f"Sprache '{lang}' hat keine Stimme")
    if not PIPER_VOICES[lang]:
        raise HTTPException(404, f"Sprache '{lang}' hat keine Piper-Stimme "
                                 "(Texte vorhanden, Audio folgt evtl. später)")
    voice_key = _stimme_namen(lang, voice)
    if length_scale is not None and not 0.5 <= length_scale <= 2.0:
        raise HTTPException(400, "length_scale zwischen 0.5 und 2.0")
    wort = word.strip()
    if not ANHOEREN_RE.fullmatch(wort):
        raise HTTPException(400, "Ungültiges Wort")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT w.id, w.word, l.id AS lex_id FROM word_forms w"
            " JOIN lexemes l ON l.id = w.id"
            " WHERE w.word = ? AND w.language = ?",
            (wort, lang),
        ).fetchone()
        db_file = None
        if row:
            voice_cfg = PIPER_VOICES[lang][voice_key]
            ls = voice_cfg["length_scale"] if length_scale is None \
                else length_scale
            db_file = _audio_als_db_zeile(conn, row["id"], voice_key, ls,
                                          _default_voice(lang))
    finally:
        conn.close()
    voice = PIPER_VOICES[lang][voice_key]
    ls = voice["length_scale"] if length_scale is None else length_scale
    # Abweichendes Tempo bekommt eigenen Cache-Eintrag (Test-Einstellungen
    # verunreinigen nicht den Standard-Cache).
    ls_tag = "" if ls == voice["length_scale"] else f"-ls{ls:g}"
    if row:
        if db_file and (AUDIO_DIR / db_file).is_file():
            return FileResponse(AUDIO_DIR / db_file, media_type="audio/wav",
                                filename=f"{wort}.wav")
        out = AUDIO_DIR / f"{lang}-{row['id']}-{voice_key}{ls_tag}.wav"
        text = row["word"]
        lex_id = row["lex_id"]
        form_id = row["id"]
    else:
        h = hashlib.sha1(wort.encode()).hexdigest()[:10]
        out = AUDIO_DIR / f"_t-{lang}-{voice_key}{ls_tag}-{h}.wav"
        text = wort
        lex_id = None
        form_id = None
    if not out.is_file():
        _synth_wort(text, lang, voice_key, out, length_scale)
        if form_id is not None:
            conn2 = get_conn()
            try:
                _audio_dbschreiben(conn2, form_id, lex_id, voice_key, ls,
                                   out.name)
                conn2.commit()
            finally:
                conn2.close()
    return FileResponse(out, media_type="audio/wav", filename=f"{text}.wav")


class StimmeVoteIn(BaseModel):
    language: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")
    voice: str = Field(min_length=1, max_length=40)
    gut: bool


@app.post("/stimme/bewerten", status_code=201)
def stimme_bewerten(vote: StimmeVoteIn, request: Request,
                    response: Response):
    """Sprecher bewerten (👍 gut / 👎 nicht brauchbar), eine Stimme pro
    Sitzung und Sprecher (Cookie-Kennung, kein Merkmal)."""
    lang = vote.language.lower()
    if lang not in PIPER_VOICES or vote.voice not in PIPER_VOICES[lang]:
        raise HTTPException(404, "Sprecher nicht bekannt")
    sid = _get_session(request, response)
    conn = get_conn()
    try:
        dup = conn.execute(
            "SELECT 1 FROM speaker_ratings WHERE language = ? AND voice = ?"
            " AND session_id = ?",
            (lang, vote.voice, sid),
        ).fetchone()
        if dup:
            raise HTTPException(409, "Diese Sitzung hat diesen Sprecher "
                                     "schon bewertet")
        conn.execute(
            "INSERT INTO speaker_ratings (language, voice, gut, session_id)"
            " VALUES (?, ?, ?, ?)",
            (lang, vote.voice, 1 if vote.gut else 0, sid),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/stimmen")
def stimmen():
    """Stimmen je Sprache mit Label, Modell-Status, Bewertungsbilanz und
    Beispielwörtern (mit Bild) — Datengrundlage der /sprecher-Testseite."""
    conn = get_conn()
    try:
        result = []
        for lang, voices in PIPER_VOICES.items():
            if not voices:
                result.append({"language": lang, "voices": [],
                               "note": "keine Piper-Stimme verfügbar"})
                continue
            rows = conn.execute(
                "SELECT voice,"
                " SUM(CASE WHEN gut=1 THEN 1 ELSE 0 END) gut,"
                " SUM(CASE WHEN gut=0 THEN 1 ELSE 0 END) nicht "
                "FROM speaker_ratings WHERE language = ? GROUP BY voice",
                (lang,),
            ).fetchall()
            bilanz = {r["voice"]: {"gut": r["gut"] or 0, "nicht": r["nicht"] or 0}
                      for r in rows}
            examples = [
                {"word": r["word"], "image": r["image"]}
                for r in conn.execute(
                    "SELECT w.word,"
                    " (SELECT filename FROM lexeme_images li"
                    "  WHERE li.lexeme_id = w.id AND li.is_main = 1) AS image"
                    " FROM word_forms w WHERE w.language = ?"
                    " ORDER BY w.id LIMIT 4", (lang,)).fetchall()
            ]
            voice_list = []
            for key, v in voices.items():
                model_file = PIPER_DIR / "voices" / v["model"]
                voice_list.append({
                    "key": key, "label": v["label"],
                    "model": v["model"],
                    "model_present": model_file.is_file(),
                    "length_scale": v["length_scale"],
                    "default": key == _default_voice(lang),
                    "bewertung": bilanz.get(key, {"gut": 0, "nicht": 0}),
                })
            result.append({"language": lang, "voices": voice_list,
                           "beispiele": examples})
        return result
    finally:
        conn.close()


class VorwaermenIn(BaseModel):
    language: str = Field(min_length=2, max_length=10, pattern=r"^[a-z]+$")
    voice: str | None = Field(default=None, max_length=40)


_vorwaermen_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_vorwaermen_state: dict[str, str] = {}


def _vorwaermen_job(lang: str, voice: str | None) -> None:
    """Hintergrund: fehlende Audiodateien für Sprache (alle Stimmen oder
    eine) nachgenerieren. Fehlende Datei = offene Aufgabe: abgebrochene
    Läufe lassen sich jederzeit fortsetzen."""
    voices = [voice] if voice else list(PIPER_VOICES[lang])
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT w.id, w.word, l.id AS lex_id FROM word_forms w"
                " JOIN lexemes l ON l.id = w.id WHERE w.language = ?",
                (lang,)).fetchall()
        finally:
            conn.close()
        for vkey in voices:
            v = PIPER_VOICES[lang][vkey]
            ls_tag = "" if v["length_scale"] == 1.0 else \
                f"-ls{v['length_scale']:g}"
            for row in rows:
                out = AUDIO_DIR / f"{lang}-{row['id']}-{vkey}{ls_tag}.wav"
                if out.is_file():
                    continue
                try:
                    _synth_wort(row["word"], lang, vkey, out)
                    conn2 = get_conn()
                    try:
                        _audio_dbschreiben(conn2, row["id"], row["lex_id"],
                                           vkey, v["length_scale"], out.name)
                        conn2.commit()
                    finally:
                        conn2.close()
                except HTTPException:
                    pass  # einzelner Fehler stoppt den Lauf nicht
        _vorwaermen_state[lang] = "fertig"
    except Exception as e:
        _vorwaermen_state[lang] = f"fehler: {e}"
    finally:
        _vorwaermen_locks[lang].release()


@app.post("/audio/vorwaermen", status_code=202)
def audio_vorwaermen(body: VorwaermenIn, _=Depends(require_api_key)):
    """Fehlende Wort-Audios für eine Sprache vorgenerieren (Hintergrund).
    Restart-fähig: bereits vorhandene Dateien werden übersprungen."""
    lang = body.language.lower()
    if lang not in PIPER_VOICES or not PIPER_VOICES[lang]:
        raise HTTPException(404, f"Sprache '{lang}' hat keine Stimme")
    voice = None
    if body.voice is not None:
        if body.voice not in PIPER_VOICES[lang]:
            raise HTTPException(404, f"Sprecher '{body.voice}' nicht bekannt")
        voice = body.voice
    lock = _vorwaermen_locks[lang]
    if not lock.acquire(blocking=False):
        raise HTTPException(409,
                            f"Vorgenerierung für '{lang}' läuft bereits")
    _vorwaermen_state[lang] = "läuft"
    threading.Thread(target=_vorwaermen_job, args=(lang, voice),
                     daemon=True).start()
    return {"gestartet": True, "language": lang,
            "status": _vorwaermen_state}


@app.get("/audio/status")
def audio_status(_=Depends(require_api_key)):
    """Fortschritt der Vorgenerierung: Dateianzahl je Sprach-Prefix."""
    counts: dict[str, int] = {}
    for f in AUDIO_DIR.glob("*.wav"):
        prefix = f.name.split("-")[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    return {"zustaende": dict(_vorwaermen_state), "dateien": counts}


# ---------- Bild setzen (Pipeline) ----------

@app.post("/entries/{entry_id}/image", response_model=EntryOut)
def set_image(entry_id: int, body: ImageIn, _=Depends(require_api_key)):
    """Bild (Base64) für einen Eintrag hochladen — für die Bild-Pipeline."""
    try:
        data = base64.b64decode(body.data_b64, validate=True)
    except Exception:
        raise HTTPException(400, "Ungültiges Base64")
    if len(data) > 2_000_000:
        raise HTTPException(413, "Bild zu groß (max. 2 MB)")
    conn = get_conn()
    try:
        old = _main_image(conn, entry_id)
        if old is None:
            exists = conn.execute(
                "SELECT 1 FROM lexemes WHERE id = ?", (entry_id,)).fetchone()
            if not exists:
                raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        if (old or "") != body.filename:
            # Neue Bildversion: alte Bewertungen bezogen sich auf das
            # vorherige Bild -> zurücksetzen, damit das neue fair bewertet wird
            conn.execute("DELETE FROM ratings WHERE lexeme_id = ?",
                         (entry_id,))
        # Galerie-Zeitstrahl: Neuzugänge (/bilder/neu) leben von dieser
        # Verknüpfungs-Zeile — UNIQUE(lexeme_id, filename) macht sie idempotent
        conn.execute(
            "INSERT OR IGNORE INTO lexeme_images (lexeme_id, filename) "
            "VALUES (?,?)", (entry_id, body.filename))
        _set_main_image(conn, entry_id, body.filename)
        conn.commit()
        (IMAGES_DIR / body.filename).write_bytes(data)
        row = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.id = ?", (entry_id,)
        ).fetchone()
        return _entry_from_row(conn, row)
    finally:
        conn.close()


@app.post("/entries/{entry_id}/image-ref", response_model=EntryOut)
def set_image_ref(entry_id: int, body: ImageIn, _=Depends(require_api_key)):
    """Bildfeld auf eine BEREITS auf dem Server liegende Datei zeigen —
    zum Zurückholen von Einträgen ohne erneuten Upload."""
    if not (IMAGES_DIR / body.filename).is_file():
        raise HTTPException(404, "Bilddatei nicht auf dem Server")
    conn = get_conn()
    try:
        old = _main_image(conn, entry_id)
        if old is None:
            exists = conn.execute(
                "SELECT 1 FROM lexemes WHERE id = ?", (entry_id,)).fetchone()
            if not exists:
                raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        if (old or "") != body.filename:
            conn.execute("DELETE FROM ratings WHERE lexeme_id = ?",
                         (entry_id,))
            conn.execute(
                "INSERT OR IGNORE INTO lexeme_images (lexeme_id, filename)"
                " VALUES (?,?)", (entry_id, body.filename))
            _set_main_image(conn, entry_id, body.filename)
            conn.commit()
        row = conn.execute(
            f"{_ENTRY_SELECT} WHERE w.id = ?", (entry_id,)
        ).fetchone()
        return _entry_from_row(conn, row)
    finally:
        conn.close()


@app.delete("/entries/{entry_id}", status_code=204)
def delete_entry(entry_id: int, _=Depends(require_api_key)):
    """Eintrag (Wortform + 1:1-Lexem samt abhängiger Daten) entfernen."""
    conn = get_conn()
    try:
        mid_rows = conn.execute(
            "SELECT id FROM meanings WHERE lexeme_id = ?",
            (entry_id,)).fetchall()
        for m in mid_rows:
            mid = m["id"]
            for tbl in ("meaning_definitions",
                        "meaning_links", "meaning_images", "meaning_examples"):
                conn.execute(f"DELETE FROM {tbl} WHERE meaning_id = ?",
                             (mid,))
            conn.execute(
                "DELETE FROM meaning_translations WHERE to_meaning_id = ?",
                (mid,))
            conn.execute(
                "DELETE FROM meaning_synonyms WHERE a_meaning_id = ?"
                " OR b_meaning_id = ?", (mid, mid))
        conn.execute("DELETE FROM meanings WHERE lexeme_id = ?", (entry_id,))
        for tbl in ("lexeme_defs", "lexeme_images", "lexeme_examples",
                    "lexeme_ratings", "source_links", "ratings"):
            conn.execute(f"DELETE FROM {tbl} WHERE lexeme_id = ?",
                         (entry_id,))
        conn.execute(
            "DELETE FROM lexeme_forms WHERE lexeme_id = ? OR form_id = ?",
            (entry_id, entry_id))
        rid_rows = conn.execute(
            "SELECT id FROM readings WHERE form_id = ? OR lexeme_id = ?",
            (entry_id, entry_id)).fetchall()
        for r in rid_rows:
            conn.execute("DELETE FROM reading_audio WHERE reading_id = ?",
                         (r["id"],))
            conn.execute("DELETE FROM pronunciations WHERE reading_id = ?",
                         (r["id"],))
        conn.execute(
            "DELETE FROM readings WHERE form_id = ? OR lexeme_id = ?",
            (entry_id, entry_id))
        conn.execute("DELETE FROM rating_nonces WHERE lexeme_id = ?",
                     (entry_id,))
        conn.execute("DELETE FROM image_moves WHERE from_lexeme_id = ?",
                     (entry_id,))
        cur = conn.execute("DELETE FROM lexemes WHERE id = ?", (entry_id,))
        conn.execute("DELETE FROM word_forms WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
