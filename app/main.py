"""Wörterbuch-API: Einträge anlegen, suchen, löschen — per REST.

Start:  uvicorn app.main:app --reload
Doku:   http://127.0.0.1:8000/docs  (Swagger UI)

Schutz vor Missbrauch:
  - Schreibzugriffe (POST/DELETE) brauchen den Header X-API-Key,
    sofern die Umgebungsvariable API_KEY gesetzt ist. Ist sie nicht
    gesetzt, wird beim Start ein zufälliger Key generiert und einmalig
    geloggt — die API ist dann nie unbeabsichtigt offen.
  - Rate-Limiting pro IP (in-Memory-Middleware): lesen 60/Min, schreiben
    5/Min; bekanntgültiger Key: 600/Min (für den Harvester).
    Grenzen per Umgebungsvariable anpassbar: RATE_READ_PER_MIN,
    RATE_WRITE_PER_MIN, RATE_WRITE_KEYED_PER_MIN.
    Achtung: Zähler pro Prozess — bei mehreren Workern nicht global.
  - Payload-Grenzen: Wort max. 100 Zeichen, Batch max. 500 Einträge.
"""
import base64
import json
import os
import re
import secrets
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

from .db import get_conn, row_to_dict

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
PIPER_DIR = Path(os.environ.get("PIPER_DIR", "/home/johannes/piper"))
PIPER_VOICES = {
    "de": "de_DE-thorsten-medium.onnx",
    "en": "en_GB-alan-medium.onnx",
    "es": "es_ES-davefx-medium.onnx",
    "fr": "fr_FR-siwis-medium.onnx",
    "it": "it_IT-paola-medium.onnx",
}
AUDIO_DIR = Path(os.environ.get(
    "AUDIO_DIR", Path(__file__).resolve().parent.parent / "audio"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
ANHOEREN_RE = re.compile(r"^[\wÄÖÜäöüßÀ-ÿ'’ -]{1,100}$", re.UNICODE)

app = FastAPI(title="Wörterbuch API", version="0.2.0")


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
    ja: int | None = None
    nein: int | None = None
    evidence: dict | None = None


class VoteIn(BaseModel):
    entry_id: int
    understood: bool
    # Bewertetes Bild (Galerie/Dateiname); None = Hauptbild
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
    fits: bool


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


class BatchResult(BaseModel):
    inserted: int
    duplicates: int
    errors: int


class DefinitionIn(BaseModel):
    definition: str = Field(min_length=1, max_length=1000)


class WordIn(BaseModel):
    """Wort eines Eintrags korrigieren (Tippfehler, Kodier-Müll)."""
    word: str = Field(min_length=1, max_length=100)


# ---------- Hilfsfunktionen ----------

def _fetch_existing(language: str, words: list[str]) -> set[str]:
    conn = get_conn()
    try:
        marks = ",".join("?" for _ in words)
        rows = conn.execute(
            f"SELECT word FROM entries WHERE language = ? AND word IN ({marks})",
            [language, *words],
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


def _entry(row):
    """Eintrag als Dict; evidence-JSON-Text als Dict aufbereiten,
    damit EntryOut.evidence (dict) validiert."""
    d = row_to_dict(row)
    ev = d.get("evidence")
    if isinstance(ev, str):
        try:
            d["evidence"] = json.loads(ev or "null")
        except Exception:
            d["evidence"] = None
    return d


# ---------- Endpunkte ----------

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


@app.post("/entries", response_model=EntryOut, status_code=201)
def create_entry(entry: EntryIn, _=Depends(require_api_key)):
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO entries (word, language, definition, source) VALUES (?,?,?,?)",
            (entry.word.strip(), entry.language.lower(), entry.definition, entry.source),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _entry(row)
    except Exception:
        conn.rollback()
        raise HTTPException(409, f"'{entry.word}' existiert bereits ({entry.language})")
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
            existing = _fetch_existing(lang, [e.word.strip() for e in items])
            for e in items:
                word = e.word.strip()
                if word in existing:
                    duplicates += 1
                    continue
                try:
                    conn.execute(
                        "INSERT INTO entries (word, language, definition, source) VALUES (?,?,?,?)",
                        (word, lang, e.definition, e.source),
                    )
                    existing.add(word)
                    inserted += 1
                except Exception:
                    errors += 1
        conn.commit()
    finally:
        conn.close()
    return BatchResult(inserted=inserted, duplicates=duplicates, errors=errors)


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
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
):
    sql, params = "SELECT * FROM entries WHERE 1=1", []
    if language:
        sql += " AND language = ?"
        params.append(language.lower())
    if prefix:
        # ULOWER(): SQLite foldet nur ASCII, Umlaute (Ö vs. ö) brauchen
        # die in db.py registrierte Unicode-Funktion.
        sql += " AND ULOWER(word) LIKE ULOWER(?)"
        params.append(prefix + "%")
    if q:
        sql += " AND word LIKE ?"
        params.append(f"%{q}%")
        _count_search(q, language or "")
    if undefined:
        sql += " AND (definition IS NULL OR definition = '')"
    if noimage:
        sql += " AND (image IS NULL OR image = '')"
    if nolemma:
        sql += " AND (lemma IS NULL OR lemma = '')"
    sql += " ORDER BY word LIMIT ? OFFSET ?"
    params += [limit, offset]
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        out = [row_to_dict(r) for r in rows]
        if with_evidence:
            for e in out:
                try:
                    e["evidence"] = json.loads(e.get("evidence") or "{}")
                except Exception:
                    e["evidence"] = {}
        else:
            for e in out:
                e.pop("evidence", None)
        if with_votes:
            marks = ",".join("?" for _ in out) or "NULL"
            votes = conn.execute(
                f"SELECT entry_id, "
                f"SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja, "
                f"SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein "
                f"FROM ratings WHERE entry_id IN ({marks}) "
                f"AND kind = 'image' GROUP BY entry_id",
                [e["id"] for e in out],
            ).fetchall() if out else []
            vmap = {v["entry_id"]: (v["ja"] or 0, v["nein"] or 0)
                    for v in votes}
            for e in out:
                e["ja"], e["nein"] = vmap.get(e["id"], (0, 0))
        return out
    finally:
        conn.close()


@app.get("/lookup", response_model=EntryOut)
def lookup(word: str, language: str):
    """Exaktes Nachschlagen eines Wortes in einer Sprache."""
    _count_search(word, language)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM entries WHERE word = ? AND language = ?",
            (word.strip(), language.lower()),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"'{word}' nicht gefunden ({language})")
    return _entry(row)


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
            "SELECT word FROM entries WHERE language = ?", (language.lower(),)
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
        row = conn.execute(
            "SELECT word, lemma, form FROM entries WHERE word = ? AND language = ?",
            (word.strip(), lang),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"'{word}' nicht gefunden ({lang})")
        lemma = row["lemma"] or row["word"]
        rows = conn.execute(
            "SELECT word, form FROM entries "
            "WHERE language = ? AND (lemma = ? OR word = ?) ORDER BY word",
            (lang, lemma, lemma),
        ).fetchall()
        forms = [{"word": r["word"], "form": r["form"] or ""} for r in rows]
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
            cur = conn.execute(
                "UPDATE entries SET lemma = ?, form = ? "
                "WHERE word = ? AND language = ?",
                (it.lemma.strip(), it.form.strip(),
                 it.word.strip(), it.language.lower()),
            )
            if cur.rowcount:
                updated += 1
            else:
                missing += 1
        conn.commit()
    finally:
        conn.close()
    return {"updated": updated, "missing": missing}


@app.get("/languages")
def languages():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT language, COUNT(*) AS count FROM entries GROUP BY language ORDER BY language"
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/stats")
def stats():
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        return {"total_entries": total}
    finally:
        conn.close()


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
def set_definition(entry_id: int, body: DefinitionIn, _=Depends(require_api_key)):
    """Definition eines Eintrags setzen/überschreiben (z. B. Harvester define)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE entries SET definition = ? WHERE id = ?",
            (body.definition.strip(), entry_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry(row)
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
            "SELECT language FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        dup = conn.execute(
            "SELECT 1 FROM entries WHERE word = ? AND language = ?",
            (body.word.strip(), row["language"]),
        ).fetchone()
        if dup:
            raise HTTPException(
                409, f"'{body.word.strip()}' existiert bereits "
                     f"({row['language']}) — Zusammenfuehrung noetig")
        conn.execute(
            "UPDATE entries SET word = ? WHERE id = ?",
            (body.word.strip(), entry_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry(row)
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise HTTPException(409, f"'{body.word.strip()}' existiert bereits")
    finally:
        conn.close()


# ---------- Bilder & Bewertung ----------

@app.get("/bewerten", include_in_schema=False)
def bewerten():
    """Seite zum Bewerten der Wort-Bilder."""
    return FileResponse(Path(__file__).parent / "static" / "bewerten.html")


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
                "last_seen > datetime('now', ?)", (sid, f"-{SESSION_IDLE_H} hours"),
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
    sql = ("SELECT e.id, e.word, e.language, e.image, "
           "(SELECT COUNT(*) FROM ratings r WHERE r.entry_id = e.id "
           " AND r.kind = 'image') AS votes "
           "FROM entries e WHERE e.image IS NOT NULL AND e.image != ''")
    params: list = []
    if language:
        sql += " AND e.language = ?"
        params.append(language.lower())
    seen = [int(x) for x in exclude.split(",") if x.strip().isdigit()][:300]
    if seen:
        sql += f" AND e.id NOT IN ({','.join('?' * len(seen))})"
        params += seen
    sql += " ORDER BY votes ASC, RANDOM() LIMIT 1"
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Keine neuen Bilder mehr vorhanden")
    out = row_to_dict(row)
    # Sitzung + Einmal-Nonce (an Sitzung und Eintrag gebunden)
    sid = _get_session(request, response)
    nonce = secrets.token_urlsafe(16)
    conn_n = get_conn()
    try:
        conn_n.execute(
            "DELETE FROM rating_nonces WHERE expires_at < datetime('now')")
        conn_n.execute(
            "INSERT INTO rating_nonces (nonce, session_id, entry_id, "
            "expires_at) VALUES (?, ?, ?, datetime('now', ?))",
            (nonce, sid, out["id"], f"+{NONCE_TTL_MIN} minutes"))
        conn_n.commit()
    finally:
        conn_n.close()
    out["nonce"] = nonce
    # Galerie: weitere Bilder, die den Begriff mitbeschreiben
    conn2 = get_conn()
    try:
        out["gallery"] = [r["filename"] for r in conn2.execute(
            "SELECT filename FROM entry_images "
            "WHERE entry_id = ? AND filename != ? ORDER BY id",
            (out["id"], out["image"] or "")).fetchall()]
    finally:
        conn2.close()
    # Alternative Kandidaten desselben Wortes (rendern liefert immer
    # mehrere Entwürfe; Gewinner steht in entries.image) — direkt vom
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
def zufall(language: str = "de"):
    """Zufälliger Eintrag mit Bild — Entdecken-Karte auf der Startseite."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT word, language, definition, image FROM entries "
            "WHERE language = ? AND image IS NOT NULL AND image != '' "
            "ORDER BY RANDOM() LIMIT 1", (language.lower(),)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Keine Einträge mit Bild in dieser Sprache")
    return row_to_dict(row)


@app.post("/raten/pick")
def raten_pick(pick: PickIn):
    """Kandidaten-Bild zum offiziellen Bild machen (Voting-Eingriff).

    Erlaubt nur Dateien, die zum Eintrag gehoeren: <lang>-<id>-cand<N>.png
    oder <lang>-<id>-v<N>.png, und die auf der Platte existieren.
    Das bisherige Hauptbild bleibt als Galerie-Bild erhalten — die
    Kombination mehrerer Bilder beschreibt den Begriff. Stimmen werden
    pro Datei gezaehlt: alte Hauptbild-Stimmen (image IS NULL) werden auf
    den Dateinamen des alten Hauptbilds umgebogen.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, language, image FROM entries WHERE id = ?",
            (pick.entry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {pick.entry_id} nicht gefunden")
        prefix = f"{row['language']}-{row['id']}-"
        if not pick.filename.startswith(prefix):
            raise HTTPException(400, "Datei gehört nicht zu diesem Eintrag")
        if not (IMAGES_DIR / pick.filename).is_file():
            raise HTTPException(404, "Bilddatei nicht vorhanden")
        old = row["image"] or ""
        if old != pick.filename:
            if old:
                conn.execute(
                    "INSERT OR IGNORE INTO entry_images (entry_id, filename) "
                    "VALUES (?, ?)", (pick.entry_id, old))
                conn.execute(
                    "UPDATE ratings SET image = ? WHERE entry_id = ? "
                    "AND image IS NULL", (old, pick.entry_id))
            conn.execute(
                "INSERT OR IGNORE INTO entry_images (entry_id, filename) "
                "VALUES (?, ?)", (pick.entry_id, pick.filename))
            conn.execute("UPDATE entries SET image = ? WHERE id = ?",
                         (pick.filename, pick.entry_id))
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "image": pick.filename}


@app.post("/bilder/upload", status_code=201)
def bilder_upload(body: ImageIn, _=Depends(require_api_key)):
    """Bilddatei nur speichern (ohne entries.image zu ändern) — für
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
            "SELECT 1 FROM entries WHERE id = ?", (vote.entry_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Eintrag {vote.entry_id} nicht gefunden")
        if not keyed:
            # Nonce atomar verbrauchen (gebunden an Sitzung + Eintrag)
            cur = conn.execute(
                "DELETE FROM rating_nonces WHERE nonce = ? AND session_id = ? "
                "AND entry_id = ? AND expires_at > datetime('now')",
                (vote.nonce or "", sid, vote.entry_id))
            if cur.rowcount != 1:
                raise HTTPException(403, "Ungültiger oder verbrauchter Nonce.")
            # Dasselbe Bild in derselben Sitzung nur einmal
            dup = conn.execute(
                "SELECT 1 FROM ratings WHERE session_id = ? AND entry_id = ? "
                "AND COALESCE(image, '') = COALESCE(?, '')",
                (sid, vote.entry_id, vote.image)).fetchone()
            if dup:
                raise HTTPException(409, "Bereits bewertet.")
        conn.execute(
            "INSERT INTO ratings (entry_id, understood, image, kind, "
            "session_id) VALUES (?, ?, ?, ?, ?)",
            (vote.entry_id, 1 if vote.understood else 0, vote.image,
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
            "SELECT id, language, image FROM entries WHERE id = ?",
            (move.entry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {move.entry_id} nicht gefunden")
        mine = {r["filename"] for r in conn.execute(
            "SELECT filename FROM entry_images WHERE entry_id = ?",
            (move.entry_id,)).fetchall()}
        if row["image"]:
            mine.add(row["image"])
        if move.filename not in mine:
            raise HTTPException(400,
                                "Datei gehört nicht zu diesem Eintrag")
        target = conn.execute(
            "SELECT id FROM entries WHERE word = ? AND language = ?",
            (move.target_word.strip(), row["language"]),
        ).fetchone()
        if not target:
            raise HTTPException(404,
                                f"'{move.target_word}' gibt es im "
                                f"{row['language']}-Bestand nicht")
        conn.execute(
            "INSERT INTO image_moves (filename, from_entry_id, target_word,"
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
            "SELECT m.*, e.word AS from_word FROM image_moves m "
            "JOIN entries e ON e.id = m.from_entry_id "
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
            src = conn.execute(
                "SELECT image FROM entries WHERE id = ?",
                (m["from_entry_id"],)).fetchone()
            is_main = bool(src and src["image"] == m["filename"])
            if is_main:
                nxt = conn.execute(
                    "SELECT filename FROM entry_images "
                    "WHERE entry_id = ? AND filename != ? ORDER BY id LIMIT 1",
                    (m["from_entry_id"], m["filename"])).fetchone()
                conn.execute(
                    "UPDATE entries SET image = ? WHERE id = ?",
                    (nxt["filename"] if nxt else None, m["from_entry_id"]))
            conn.execute(
                "DELETE FROM entry_images WHERE entry_id = ? AND filename = ?",
                (m["from_entry_id"], m["filename"]))
            # Stimmen im alten Kontext verwirren nur
            conn.execute(
                "DELETE FROM ratings WHERE entry_id = ? AND image = ?",
                (m["from_entry_id"], m["filename"]))
            tgt = conn.execute(
                "SELECT id, image FROM entries WHERE word = ? AND language = ?",
                (m["target_word"], m["language"])).fetchone()
            if not tgt:
                raise HTTPException(404, "Zieleintrag nicht gefunden")
            conn.execute(
                "INSERT OR IGNORE INTO entry_images (entry_id, filename) "
                "VALUES (?, ?)", (tgt["id"], m["filename"]))
            if not tgt["image"]:
                conn.execute(
                    "UPDATE entries SET image = ? WHERE id = ?",
                    (m["filename"], tgt["id"]))
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
            "SELECT e.id, e.word, e.language, e.definition, e.image, "
            "SUM(CASE WHEN r.understood = 1 THEN 1 ELSE 0 END) AS ja, "
            "SUM(CASE WHEN r.understood = 0 THEN 1 ELSE 0 END) AS nein "
            "FROM ratings r JOIN entries e ON e.id = r.entry_id "
            "WHERE r.kind = 'image' "
            "GROUP BY r.entry_id "
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
            "SELECT COUNT(*) AS n FROM entries WHERE image IS NOT NULL AND image != ''"
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"votes": votes, "understood": understood,
            "not_understood": votes - understood, "unknown": unknown,
            "entries_with_image": with_image}


# ---------- Wort-Konfidenz & Evidenz ----------

@app.post("/wort/vote", status_code=201)
def wort_vote(vote: WordVoteIn):
    """Öffentliche Kuratier-Stimme: Gehört das Wort zur Sprache?

    Unabhängig vom Bild-Voting — ein Wort kann ein gutes Bild haben
    und trotzdem fehl am Platz sein (und umgekehrt).
    """
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM entries WHERE id = ?", (vote.entry_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Eintrag {vote.entry_id} nicht gefunden")
        conn.execute(
            "INSERT INTO word_ratings (entry_id, fits) VALUES (?, ?)",
            (vote.entry_id, 1 if vote.fits else 0),
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
            "SELECT id, word, language, definition, image, source,"
            "       confidence, evidence, sprache_unsicher, created_at "
            "FROM entries WHERE id = ?", (entry_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        img = conn.execute(
            "SELECT SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja,"
            "       SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein "
            "FROM ratings WHERE entry_id = ? AND kind = 'image'", (entry_id,),
        ).fetchone()
        wr = conn.execute(
            "SELECT SUM(CASE WHEN fits=1 THEN 1 ELSE 0 END) passt,"
            "       SUM(CASE WHEN fits=0 THEN 1 ELSE 0 END) passt_nicht "
            "FROM word_ratings WHERE entry_id = ?", (entry_id,),
        ).fetchone()
        unbekannt = conn.execute(
            "SELECT COUNT(*) AS n FROM ratings "
            "WHERE entry_id = ? AND kind = 'unknown'", (entry_id,),
        ).fetchone()["n"]
        # Stimmen pro Bilddatei (NULL/'' = Hauptbild)
        per_img = {}
        for r in conn.execute(
                "SELECT COALESCE(image, '') AS img,"
                " SUM(CASE WHEN understood=1 THEN 1 ELSE 0 END) ja,"
                " SUM(CASE WHEN understood=0 THEN 1 ELSE 0 END) nein "
                "FROM ratings WHERE entry_id = ? AND kind = 'image' "
                "GROUP BY img",
                (entry_id,)).fetchall():
            per_img[r["img"]] = {"ja": r["ja"] or 0, "nein": r["nein"] or 0}
        gallery = [g["filename"] for g in conn.execute(
            "SELECT filename FROM entry_images WHERE entry_id = ? "
            "AND filename != ? ORDER BY id",
            (entry_id, row["image"] or "")).fetchall()]
    finally:
        conn.close()
    out = row_to_dict(row)
    try:
        out["evidence"] = json.loads(out["evidence"] or "{}")
    except Exception:
        out["evidence"] = {}
    out["image_votes"] = {"ja": img["ja"] or 0, "nein": img["nein"] or 0}
    out["word_votes"] = {"passt": wr["passt"] or 0,
                         "passt_nicht": wr["passt_nicht"] or 0}
    # "Wort unbekannt"-Stimmen der Bewertungsseite — eigenes Signal:
    # verstehen Menschen den Begriff, unabhaengig vom Bild?
    out["unknown_votes"] = unbekannt
    # Bilder des Eintrags: Hauptbild zuerst, dann Galerie — jeweils mit
    # ihren eigenen Stimmen (Kombination beschreibt den Begriff).
    bilder = []
    if row["image"]:
        v = per_img.get(row["image"], per_img.get("", {"ja": 0, "nein": 0}))
        bilder.append({"filename": row["image"], "main": True, **v})
    for fn in gallery:
        v = per_img.get(fn, {"ja": 0, "nein": 0})
        bilder.append({"filename": fn, "main": False, **v})
    out["bilder"] = bilder
    return out


@app.post("/entries/{entry_id}/evidence", response_model=EntryOut)
def set_evidence(entry_id: int, body: EvidenceIn,
                 _=Depends(require_api_key)):
    """Konfidenz + Evidenz speichern (Pruef-Pipeline)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE entries SET confidence = ?, evidence = ?,"
            " sprache_unsicher = ? WHERE id = ?",
            (body.confidence, json.dumps(body.evidence, ensure_ascii=False),
             1 if body.sprache_unsicher else 0, entry_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry(row)
    finally:
        conn.close()


# ---------- Anhören (Piper TTS) ----------

@app.get("/anhoeren")
def anhoeren(word: str, language: str = "de"):
    """Wort aussprechen (Piper auf dem Pi). Gecacht pro Eintrag:
    audio/<lang>-<id>.wav — Synthese nur beim ersten Aufruf."""
    lang = language.lower()
    if lang not in PIPER_VOICES:
        raise HTTPException(404, f"Sprache '{lang}' hat keine Stimme")
    wort = word.strip()
    if not ANHOEREN_RE.fullmatch(wort):
        raise HTTPException(400, "Ungültiges Wort")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, word FROM entries WHERE word = ? AND language = ?",
            (wort, lang),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"'{wort}' nicht gefunden ({lang})")
    out = AUDIO_DIR / f"{lang}-{row['id']}.wav"
    if not out.is_file():
        model = PIPER_DIR / "voices" / PIPER_VOICES[lang]
        piper = PIPER_DIR / "piper" / "piper"
        if not piper.is_file() or not model.is_file():
            raise HTTPException(503, "Sprachausgabe auf diesem Server "
                                     "nicht eingerichtet")
        tmp = out.with_suffix(".tmp.wav")
        try:
            proc = subprocess.run(
                [str(piper), "--model", str(model),
                 "--output_file", str(tmp)],
                input=(row["word"] + "\n").encode(),
                capture_output=True, timeout=60)
            if proc.returncode != 0 or not tmp.is_file():
                raise HTTPException(500,
                                    "Synthese fehlgeschlagen: "
                                    + proc.stderr.decode(errors="replace")
                                    [:300])
            tmp.rename(out)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Synthesefehler: {e}")
    return FileResponse(out, media_type="audio/wav",
                        filename=f"{row['word']}.wav")


@app.get("/worteintrag", include_in_schema=False)
def worteintrag():
    """Wort-Detailseite: Konfidenzbalken, Evidenz, Votes, Formen."""
    return FileResponse(Path(__file__).parent / "static" / "wort.html")


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
        old = conn.execute(
            "SELECT image FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if old is None:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        if (old["image"] or "") != body.filename:
            # Neue Bildversion: alte Bewertungen bezogen sich auf das
            # vorherige Bild -> zurücksetzen, damit das neue fair bewertet wird
            conn.execute("DELETE FROM ratings WHERE entry_id = ?", (entry_id,))
        conn.execute(
            "UPDATE entries SET image = ? WHERE id = ?",
            (body.filename, entry_id),
        )
        conn.commit()
        (IMAGES_DIR / body.filename).write_bytes(data)
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry(row)
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
        old = conn.execute(
            "SELECT image FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if old is None:
            raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
        if (old["image"] or "") != body.filename:
            conn.execute("DELETE FROM ratings WHERE entry_id = ?", (entry_id,))
            conn.execute(
                "UPDATE entries SET image = ? WHERE id = ?",
                (body.filename, entry_id),
            )
            conn.commit()
        row = conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry(row)
    finally:
        conn.close()


@app.delete("/entries/{entry_id}", status_code=204)
def delete_entry(entry_id: int, _=Depends(require_api_key)):
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
