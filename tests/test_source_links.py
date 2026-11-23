"""Tests fuer gepruefte Externquellen-Links (source_links)."""
import os
import tempfile
from pathlib import Path

os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-links"

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


def test_source_link_put_und_evidence():
    eid = _eintrag("Apfelbaum")
    url = "https://de.wiktionary.org/wiki/Apfelbaum"
    r = client.post(f"/entries/{eid}/source-links",
                   json={"kind": "wiktionary", "url": url}, headers=KEY)
    assert r.status_code == 200, r.text
    r = client.post(f"/entries/{eid}/source-links",
                   json={"kind": "wiktionary", "url": url}, headers=KEY)
    assert r.status_code == 200  # Upsert: kein Fehler beim Wiederholen
    ev = client.get(f"/entries/{eid}/evidence").json()
    assert {"kind": "wiktionary", "url": url} in ev["source_links"]


def test_source_link_ohne_key_verweigert():
    eid = _eintrag("Birnbaum")
    r = client.post(f"/entries/{eid}/source-links",
                   json={"kind": "wiktionary",
                         "url": "https://de.wiktionary.org/wiki/Birnbaum"})
    assert r.status_code == 401


def test_source_link_nur_https():
    eid = _eintrag("Kirschbaum")
    r = client.post(f"/entries/{eid}/source-links",
                   json={"kind": "wiktionary", "url": "http://boese.example"},
                   headers=KEY)
    assert r.status_code == 400


def test_nolinks_filter():
    voll = _eintrag("Vollstaendig")
    leer1 = _eintrag("LeerEins")
    leer2 = _eintrag("LeerZwei")
    for kind, u in (("wiktionary", "https://de.wiktionary.org/wiki/Vollstaendig"),
                    ("wikipedia", "https://de.wikipedia.org/wiki/Vollstaendig"),
                    ("dwds", "https://www.dwds.de/wb/Vollstaendig")):
        client.post(f"/entries/{voll}/source-links",
                    json={"kind": kind, "url": u}, headers=KEY)
    # geteilter Test-DB: auch Einträge aus anderen Testmodulen stehen
    # in der Tabelle — Limit hochsetzen, damit unsere Wörter drin sind.
    r = client.get("/entries", params={"nolinks": True, "limit": 1000})
    ids = {e["id"] for e in r.json()}
    assert leer1 in ids and leer2 in ids and voll not in ids
