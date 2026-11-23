"""Tests für mehrsprachige Bedeutungen (definitions-Tabelle)."""
import os
import tempfile
from pathlib import Path

os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-defs"

import app.db as db
from app.main import app, API_KEY

from fastapi.testclient import TestClient

_tmp = tempfile.TemporaryDirectory()
db.DB_PATH = Path(_tmp.name) / "test.db"

client = TestClient(app)
KEY = {"X-API-Key": API_KEY} if API_KEY else {}


def _eintrag(word, lang):
    r = client.post("/entries", json={"word": word, "language": lang},
                    headers=KEY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_patch_definition_spiegelt_de():
    wid = _eintrag("accampamento", "it")
    r = client.patch(f"/entries/{wid}",
                     json={"definition": "sostantivo, m: campeggio"},
                     headers=KEY)
    assert r.status_code == 200
    r = client.get(f"/entries/{wid}/definitions")
    assert r.status_code == 200
    assert r.json()["definitions"]["de"] == "sostantivo, m: campeggio"


def test_put_definition_andere_sprache():
    wid = _eintrag("accampamento2", "it")
    r = client.put(f"/entries/{wid}/definitions",
                   json={"lang": "it",
                         "text": "sostantivo, m: luogo di campeggio"},
                   headers=KEY)
    assert r.status_code == 200
    d = client.get(f"/entries/{wid}/definitions").json()["definitions"]
    assert d["it"] == "sostantivo, m: luogo di campeggio"
    assert "de" not in d


def test_put_de_syncs_entries():
    wid = _eintrag("Hausdefs", "de")
    client.put(f"/entries/{wid}/definitions",
               json={"lang": "de", "text": "Substantiv: Gebäude"},
               headers=KEY)
    r = client.get("/entries", params={"language": "de", "prefix": "hausdef",
                                       "limit": 10})
    assert r.json()[0]["definition"] == "Substantiv: Gebäude"


def test_no_def_filter():
    a = _eintrag("alphaone", "it")
    b = _eintrag("bravoone", "it")
    client.put(f"/entries/{a}/definitions",
               json={"lang": "it", "text": "sostantivo: alpha"},
               headers=KEY)
    r = client.get("/entries", params={"language": "it", "no_def": "it",
                                       "limit": 100})
    woerter = {e["word"] for e in r.json()}
    assert "bravoone" in woerter
    assert "alphaone" not in woerter


def test_definitions_404():
    r = client.get("/entries/999999/definitions")
    assert r.status_code == 404
