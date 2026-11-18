"""Wörterbuch-API: Einträge anlegen, suchen, löschen — per REST.

Start:  uvicorn app.main:app --reload
Doku:   http://127.0.0.1:8000/docs  (Swagger UI)
"""
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .db import get_conn, row_to_dict

app = FastAPI(title="Wörterbuch API", version="0.1.0")


# ---------- Schemas ----------

class EntryIn(BaseModel):
    word: str = Field(min_length=1)
    language: str = Field(min_length=2, max_length=10, description="z. B. 'de', 'en'")
    definition: str | None = None
    source: str = "manual"


class EntryOut(BaseModel):
    id: int
    word: str
    language: str
    definition: str | None
    source: str
    created_at: str


class BatchIn(BaseModel):
    entries: list[EntryIn]


class BatchResult(BaseModel):
    inserted: int
    duplicates: int
    errors: int


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

@app.post("/entries", response_model=EntryOut, status_code=201)
def create_entry(entry: EntryIn):
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
def create_batch(batch: BatchIn):
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
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
):
    sql, params = "SELECT * FROM entries WHERE 1=1", []
    if language:
        sql += " AND language = ?"
        params.append(language.lower())
    if prefix:
        sql += " AND word LIKE ?"
        params.append(prefix + "%")
    if q:
        sql += " AND word LIKE ?"
        params.append(f"%{q}%")
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


@app.delete("/entries/{entry_id}", status_code=204)
def delete_entry(entry_id: int):
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, f"Eintrag {entry_id} nicht gefunden")
