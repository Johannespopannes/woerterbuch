"""Tests fuer das autonome Definitions-Sprachaudit (Legacy-de)."""
import os
import tempfile
from pathlib import Path

os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-audit"

import app.db as db
from app.main import app, API_KEY

from fastapi.testclient import TestClient

_tmp = tempfile.TemporaryDirectory()
db.DB_PATH = Path(_tmp.name) / "test.db"

client = TestClient(app)
KEY = {"X-API-Key": API_KEY} if API_KEY else {}


def _eintrag(word, lang="de", definition=None):
    r = client.post("/entries", json={"word": word, "language": lang},
                    headers=KEY)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    if definition is not None:
        client.put(f"/entries/{eid}/definitions",
                   json={"lang": lang, "text": definition}, headers=KEY)
    return eid


def test_audit_queue_liefert_nur_legacy():
    a = _eintrag("auditqueue", "de", "ein Testwort")
    # echtes Legacy-Markierungen setzen (wie der Import)
    client.put(f"/entries/{a}/definitions",
               json={"lang": "de", "text": "ein Testwort",
                     "source": "legacy"}, headers=KEY)
    _eintrag("schon_geprueft", "de", "auch ein Test")
    rows = client.get("/definitions/audit-queue?limit=200").json()["rows"]
    ids = {r["entry_id"] for r in rows}
    assert a in ids
    client.post(f"/entries/{a}/definitions/audit",
                json={"action": "confirm"}, headers=KEY)
    rows = client.get("/definitions/audit-queue?limit=200").json()["rows"]
    ids = {r["entry_id"] for r in rows}
    assert a not in ids  # durch Audit aus der Queue genommen


def test_confirm_setzt_herkunft():
    eid = _eintrag("confirmtest", "de", "deutsche Bedeutung")
    r = client.post(f"/entries/{eid}/definitions/audit",
                    json={"action": "confirm", "source": "legacy-checked"},
                    headers=KEY)
    assert r.status_code == 200, r.text
    defs = client.get(f"/entries/{eid}/definitions").json()
    assert defs["sources"]["de"] == "legacy-checked"


def test_move_biegt_sprache_um():
    eid = _eintrag("movetest", "de", "significato italiano")
    r = client.post(f"/entries/{eid}/definitions/audit",
                    json={"action": "move", "lang": "it",
                          "source": "legacy-moved", "clear_entry_def": True},
                    headers=KEY)
    assert r.status_code == 200, r.text
    defs = client.get(f"/entries/{eid}/definitions").json()
    assert "it" in defs["definitions"]
    assert defs["sources"]["it"] == "legacy-moved"
    ev = client.get(f"/entries/{eid}/evidence").json()
    assert ev["definition"] in (None, "")  # deutscher Slot frei


def test_move_mit_kollision_wird_dublette():
    eid = _eintrag("duptest", "de", "Bedeutung X")
    client.put(f"/entries/{eid}/definitions",
               json={"lang": "it", "text": "significato esistente",
                     "source": "model"}, headers=KEY)
    client.post(f"/entries/{eid}/definitions/audit",
                json={"action": "move", "lang": "it"}, headers=KEY)
    defs = client.get(f"/entries/{eid}/definitions").json()
    # bestehende it-Definition darf NICHT überschrieben werden
    assert defs["definitions"]["it"] == "significato esistente"
    assert defs["sources"]["de"] == "legacy-dup"


def test_move_ohne_legacy_zeile_ist_404():
    eid = _eintrag("keinedef", "de")
    r = client.post(f"/entries/{eid}/definitions/audit",
                    json={"action": "confirm"}, headers=KEY)
    assert r.status_code == 404


def test_uns_action_abgelehnt():
    eid = _eintrag("actiontest", "de", "irgendwas")
    r = client.post(f"/entries/{eid}/definitions/audit",
                    json={"action": "loeschen"}, headers=KEY)
    assert r.status_code == 422
