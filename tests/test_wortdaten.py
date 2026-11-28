"""Tests fuer das generische Wortdaten-Bundle (/wortdaten) und die
explizite Ziel-Bedeutung bei Übersetzungsverbindungen."""
import os
import tempfile
from pathlib import Path

os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-wortdaten"

import app.db as db
from app.main import app, API_KEY

from fastapi.testclient import TestClient

_tmp = tempfile.TemporaryDirectory()
db.DB_PATH = Path(_tmp.name) / "test.db"

client = TestClient(app)
KEY = {"X-API-Key": API_KEY} if API_KEY else {}


def _eintrag(word, lang="de"):
    r = client.post("/entries", json={"word": word, "language": lang},
                    headers=KEY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_wortdaten_bundle_struktur():
    eid = _eintrag("bundletest", "de")
    sid = client.post(f"/entries/{eid}/senses",
                      json={"label": "Prüfung", "word_class": "Substantiv"},
                      headers=KEY).json()["sense_id"]
    client.put(f"/senses/{sid}/definitions",
               json={"lang": "de", "text": "Testbeschreibung"},
               headers=KEY)
    client.patch(f"/entries/{eid}/definitions",
                 json={"lang": "de", "text": "Wortebene"}, headers=KEY)
    b = client.get("/wortdaten", params={"word": "bundletest",
                                         "language": "de"}).json()
    assert b["form"]["word"] == "bundletest"
    assert len(b["lexemes"]) == 1
    lex = b["lexemes"][0]
    for feld in ("id", "lemma", "word_class", "attrs", "status", "source",
                 "created_at", "definitions", "images", "examples",
                 "grammar", "meanings", "readings", "source_links",
                 "ratings"):
        assert feld in lex, f"Bundle fehlt: {feld}"
    assert lex["definitions"]["de"]["text"] == "Wortebene"
    assert lex["images"] == {"main": None, "gallery": []}
    m = lex["meanings"][0]
    assert m["definitions"]["de"] == "Testbeschreibung"
    assert m["word_class"] == "Substantiv"
    r = lex["ratings"]
    for feld in ("image", "unknown", "word", "per_image"):
        assert feld in r


def test_patch_alias_definition():
    eid = _eintrag("patchtest", "de")
    r = client.patch(f"/entries/{eid}/definitions",
                     json={"lang": "de", "text": "per PATCH"}, headers=KEY)
    assert r.status_code == 200, r.text
    r = client.get(f"/entries/{eid}/definitions")
    assert r.json()["definitions"]["de"] == "per PATCH"


def test_mehrdeutiges_ziel_409_und_target_sense_id():
    de = _eintrag("zielbank", "de")
    en = _eintrag("targetbank", "en")
    s_de = client.post(f"/entries/{de}/senses",
                       json={"label": "Finanzinstitut",
                             "word_class": "Substantiv"},
                       headers=KEY).json()["sense_id"]
    s_en = client.post(f"/entries/{en}/senses",
                       json={"label": "institution",
                             "word_class": "Substantiv"},
                       headers=KEY).json()["sense_id"]
    client.post(f"/entries/{en}/senses",
                json={"label": "seat", "word_class": "Substantiv"},
                headers=KEY)
    # ohne Zielauswahl: 409 (keine erfundene Zuordnung)
    r = client.post(f"/senses/{s_de}/translations",
                    json={"entry_id": en}, headers=KEY)
    assert r.status_code == 409
    # mit expliziter Ziel-Bedeutung: 201
    r = client.post(f"/senses/{s_de}/translations",
                    json={"entry_id": en, "target_sense_id": s_en},
                    headers=KEY)
    assert r.status_code == 201, r.text
    # fremde Ziel-Bedeutung: 404
    r = client.post(f"/senses/{s_de}/translations",
                    json={"entry_id": en, "target_sense_id": 999999},
                    headers=KEY)
    assert r.status_code == 404


def test_translate_queue_liefert_word_class():
    eid = _eintrag("wctest", "de")
    sid = client.post(f"/entries/{eid}/senses",
                      json={"label": "Test", "word_class": "Substantiv"},
                      headers=KEY).json()["sense_id"]
    client.put(f"/senses/{sid}/definitions",
               json={"lang": "de", "text": "x"}, headers=KEY)
    rows = client.get("/senses/translate-queue",
                      params={"from": "de", "to": "en"}).json()["rows"]
    row = next(r for r in rows if r["sense_id"] == sid)
    assert row["word_class"] == "Substantiv"
