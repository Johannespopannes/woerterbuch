"""Tests für die Sprecher-/Audio-Erweiterung (Piper mehrere Stimmen)."""
import os
import tempfile
from pathlib import Path

# Rate-Limits für Tests hochsetzen (muss vor app.main-Import passieren)
os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-audio"

import app.db as db
import app.main as main_mod
from app.main import app, API_KEY

from fastapi.testclient import TestClient

_tmp = tempfile.TemporaryDirectory()
db.DB_PATH = Path(_tmp.name) / "test.db"
_test_audio = Path(_tmp.name) / "audio"
_test_audio.mkdir(parents=True, exist_ok=True)
main_mod.AUDIO_DIR = _test_audio  # Modul-Konstante im Endpoint umlenken

client = TestClient(app)
KEY = {"X-API-Key": API_KEY} if API_KEY else {}


def _eintrag(word, lang):
    r = client.post("/entries", json={"word": word, "language": lang}, headers=KEY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_stimmen_liste():
    r = client.get("/stimmen")
    assert r.status_code == 200
    data = {d["language"]: d for d in r.json()}
    # ar hat genau eine Stimme, tn keine (text-only)
    assert len(data["ar"]["voices"]) == 1
    assert data["ar"]["voices"][0]["key"] == "kareem"
    assert data["tn"]["voices"] == []
    # jede Sprache mit Stimmen hat genau einen Default
    for lang, d in data.items():
        if d["voices"]:
            assert sum(1 for v in d["voices"] if v["default"]) == 1
    assert len(data["de"]["voices"]) >= 2


def test_anhoeren_tn_404():
    _eintrag("دار", "tn")
    r = client.get("/anhoeren", params={"word": "دار", "language": "tn"})
    assert r.status_code == 404


def test_anhoeren_stimme_unbekannt():
    wid = _eintrag("Haus", "de")
    r = client.get("/anhoeren",
                   params={"word": "Haus", "language": "de", "voice": "xyz"})
    assert r.status_code == 404
    # bekannte Stimme, aber Cache-Datei fehlt -> Syntheseversuch -> 503
    # (kein Piper im Test) — vorher darf kein 500 aus Regexp/Validierung kommen
    r2 = client.get("/anhoeren",
                    params={"word": "Haus", "language": "de", "voice": "thorsten"})
    assert r2.status_code in (500, 503)


def test_anhoeren_gecached():
    wid = _eintrag("Baum", "de")
    cache = _test_audio / f"de-{wid}-thorsten.wav"
    cache.write_bytes(b"RIFF-fake")
    r = client.get("/anhoeren",
                   params={"word": "Baum", "language": "de", "voice": "thorsten"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    # arabisches Wort: Regex akzeptiert, Synthese fehlt -> 500/503, kein 400
    aid = _eintrag("بيت", "ar")
    r2 = client.get("/anhoeren",
                    params={"word": "بيت", "language": "ar"})
    assert r2.status_code in (500, 503)


def test_anhoeren_length_scale_validiert():
    wid = _eintrag("Katze", "de")
    r = client.get("/anhoeren",
                   params={"word": "Katze", "language": "de",
                           "length_scale": 9.0})
    assert r.status_code == 400


def test_stimme_bewerten_und_doppelvote():
    r = client.post("/stimme/bewerten",
                    json={"language": "de", "voice": "thorsten", "gut": True})
    assert r.status_code == 201
    r2 = client.post("/stimme/bewerten",
                     json={"language": "de", "voice": "thorsten", "gut": False})
    assert r2.status_code == 409
    r3 = client.post("/stimme/bewerten",
                     json={"language": "de", "voice": "unbekannt", "gut": True})
    assert r3.status_code == 404
    # Bilanz sichtbar
    data = {d["language"]: d for d in client.get("/stimmen").json()}
    th = [v for v in data["de"]["voices"] if v["key"] == "thorsten"][0]
    assert th["bewertung"]["gut"] == 1


def test_vorwaermen_braucht_key():
    r = client.post("/audio/vorwaermen", json={"language": "de"})
    assert r.status_code in (401, 403, 422)
