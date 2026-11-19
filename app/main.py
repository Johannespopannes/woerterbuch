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
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
            # Abstimmen auf der Bewertungsseite ist ein Write, soll aber
            # flüssig klickbar bleiben — eigener, großzügigerer Bucket.
            if request.url.path == "/raten/vote":
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


class VoteIn(BaseModel):
    entry_id: int
    understood: bool


class ImageIn(BaseModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}\.(png|webp)$")
    data_b64: str = Field(max_length=3_000_000)


class BatchIn(BaseModel):
    entries: list[EntryIn] = Field(max_length=500)


class BatchResult(BaseModel):
    inserted: int
    duplicates: int
    errors: int


class DefinitionIn(BaseModel):
    definition: str = Field(min_length=1, max_length=1000)


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
        return row_to_dict(row)
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
        return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
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
    return row_to_dict(row)


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
        return row_to_dict(row)
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


@app.get("/raten/next")
def raten_next(language: str | None = None, exclude: str = ""):
    """Bild für die Bewertungsseite: wenig bewertete zuerst, dann Zufall.

    exclude: kommagetrennte Eintrags-IDs, die diese Session schon gesehen
    hat (Client-seitige Liste, kein Server-Tracking).
    """
    sql = ("SELECT e.id, e.word, e.language, e.image, "
           "(SELECT COUNT(*) FROM ratings r WHERE r.entry_id = e.id) AS votes "
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
    return row_to_dict(row)


@app.post("/raten/vote", status_code=201)
def raten_vote(vote: VoteIn):
    """Öffentliche Bewertung: Bild verstanden oder nicht (kein Key nötig)."""
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM entries WHERE id = ?", (vote.entry_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"Eintrag {vote.entry_id} nicht gefunden")
        conn.execute(
            "INSERT INTO ratings (entry_id, understood) VALUES (?, ?)",
            (vote.entry_id, 1 if vote.understood else 0),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


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
        votes = conn.execute("SELECT COUNT(*) AS n FROM ratings").fetchone()["n"]
        understood = conn.execute(
            "SELECT COUNT(*) AS n FROM ratings WHERE understood = 1"
        ).fetchone()["n"]
        with_image = conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE image IS NOT NULL AND image != ''"
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"votes": votes, "understood": understood,
            "not_understood": votes - understood, "entries_with_image": with_image}


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
        return row_to_dict(row)
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
