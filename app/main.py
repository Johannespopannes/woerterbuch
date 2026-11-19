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
import os
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
RATE_READ = int(os.environ.get("RATE_READ_PER_MIN", "60"))
RATE_WRITE = int(os.environ.get("RATE_WRITE_PER_MIN", "5"))
RATE_WRITE_KEYED = int(os.environ.get("RATE_WRITE_KEYED_PER_MIN", "600"))
WINDOW = 60  # Sekunden

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
    source: str
    created_at: str


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
    if undefined:
        sql += " AND (definition IS NULL OR definition = '')"
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
