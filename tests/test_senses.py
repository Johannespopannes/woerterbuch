"""Tests fuer die Sinn-Ebene (Wort -> Bedeutung -> Uebersetzung)."""
import os
import tempfile
from pathlib import Path

os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-senses"

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
        client.patch(f"/entries/{eid}",
                     json={"definition": definition}, headers=KEY)
    return eid


def _sinn(eid, label):
    r = client.post(f"/entries/{eid}/senses",
                    json={"label": label, "source": "test"}, headers=KEY)
    assert r.status_code == 201, r.text
    return r.json()["sense_id"]


def test_sinn_anlegen_und_auslesen():
    eid = _eintrag("Banktest", "de", "Finanzinstitut oder Sitzmoebel")
    sid = _sinn(eid, "Finanzinstitut")
    client.put(f"/senses/{sid}/definitions",
               json={"lang": "de", "text": "Institut fuer Geldgeschaefte",
                     "source": "test"}, headers=KEY)
    client.post(f"/senses/{sid}/links",
                json={"kind": "dwds", "url": "https://www.dwds.de/wb/Banktest",
                      "claim": "mittelhochdeutsch banc", "source": "test"},
                headers=KEY)
    s = client.get(f"/entries/{eid}/senses").json()["senses"]
    assert len(s) == 1
    assert s[0]["label"] == "Finanzinstitut"
    assert s[0]["definitions"]["de"] == "Institut fuer Geldgeschaefte"
    assert s[0]["links"][0]["claim"] == "mittelhochdeutsch banc"


def test_uebersetzungsverbindung():
    bank = _eintrag("Bankverb", "de", "mehrdeutig")
    bench = _eintrag("bench", "en", "long seat")
    s1 = _sinn(bank, "Finanzinstitut")
    s2 = _sinn(bank, "Sitzmoebel")
    r = client.post(f"/senses/{s2}/translations",
                    json={"entry_id": bench, "source": "test"}, headers=KEY)
    assert r.status_code == 201, r.text
    # doppelt anlegen -> 409 (Mehrdeutigkeit gehoert auf Sinne, nicht Duplikate)
    r = client.post(f"/senses/{s2}/translations",
                    json={"entry_id": bench}, headers=KEY)
    assert r.status_code == 409
    # fehlender Zieleintrag -> 404
    r = client.post(f"/senses/{s1}/translations",
                    json={"entry_id": 999999}, headers=KEY)
    assert r.status_code == 404
    s = client.get(f"/entries/{bank}/senses").json()["senses"]
    t_s2 = next(x for x in s if x["id"] == s2)
    assert t_s2["translations"][0]["word"] == "bench"
    # entfernen -> 204, nochmal -> 404
    assert client.delete(f"/senses/{s2}/translations/{bench}",
                         headers=KEY).status_code == 204
    assert client.delete(f"/senses/{s2}/translations/{bench}",
                         headers=KEY).status_code == 404


def test_queue_nur_definierte_ohne_sinne():
    a = _eintrag("queueA", "de", "mit Definition")
    b = _eintrag("queueB", "de")  # ohne Definition
    rows = client.get("/senses/queue?language=de&limit=200").json()["rows"]
    ids = {r["id"] for r in rows}
    assert a in ids and b not in ids
    _sinn(a, "einziger Sinn")
    rows = client.get("/senses/queue?language=de&limit=200").json()["rows"]
    ids = {r["id"] for r in rows}
    assert a not in ids  # mit Sinn raus aus der Queue


def test_translate_queue():
    bank = _eintrag("Banktq", "de", "mehrdeutig")
    s1 = _sinn(bank, "Finanzinstitut")
    client.put(f"/senses/{s1}/definitions",
               json={"lang": "de", "text": "Geldinstitut"}, headers=KEY)
    rows = client.get("/senses/translate-queue?from=de&to=en&limit=50"
                      ).json()["rows"]
    assert any(r["sense_id"] == s1 for r in rows)
    bench = _eintrag("bankbench", "en", "seat")
    client.post(f"/senses/{s1}/translations",
                json={"entry_id": bench}, headers=KEY)
    # Lockerung: bis zu 3 Zielwoerter pro Zielsprache — mit 1 Verbindung
    # bleibt der Sinn in der Queue
    rows = client.get("/senses/translate-queue?from=de&to=en&limit=50"
                      ).json()["rows"]
    assert any(r["sense_id"] == s1 for r in rows)
    # 2 weitere Verbindungen -> raus aus der Queue
    for w in ("bankcorp", "bankhouse"):
        e = _eintrag(w, "en", "x")
        client.post(f"/senses/{s1}/translations",
                    json={"entry_id": e}, headers=KEY)
    rows = client.get("/senses/translate-queue?from=de&to=en&limit=50"
                      ).json()["rows"]
    assert all(r["sense_id"] != s1 for r in rows)


def test_word_class_und_approx():
    eid = _eintrag("Wortarttest", "de", "x")
    r = client.post(f"/entries/{eid}/senses",
                    json={"label": "Gerät", "source": "test",
                          "word_class": "Substantiv"}, headers=KEY)
    assert r.status_code == 201, r.text
    sid = r.json()["sense_id"]
    en = _eintrag("devicetest", "en", "y")
    r = client.post(f"/senses/{sid}/translations",
                    json={"entry_id": en, "approx": True}, headers=KEY)
    assert r.status_code == 201, r.text
    s = client.get(f"/entries/{eid}/senses").json()["senses"][0]
    assert s["word_class"] == "Substantiv"
    assert s["translations"][0]["approx"] == 1


def test_def_queue():
    eid = _eintrag("Defq", "de", "x")
    sid = _sinn(eid, "Sinn mit de")
    client.put(f"/senses/{sid}/definitions",
               json={"lang": "de", "text": "nur deutsch"}, headers=KEY)
    rows = client.get("/senses/def-queue?from=de&to=en&limit=50"
                      ).json()["rows"]
    assert any(r["sense_id"] == sid for r in rows)
    # Beschreibung auf EN nachliefern -> raus aus der Queue
    client.put(f"/senses/{sid}/definitions",
               json={"lang": "en", "text": "english only",
                     "source": "translate:de"}, headers=KEY)
    rows = client.get("/senses/def-queue?from=de&to=en&limit=50"
                      ).json()["rows"]
    assert all(r["sense_id"] != sid for r in rows)
    # def-queue von en -> de ist jetzt leer fuer diesen Sinn
    rows = client.get("/senses/def-queue?from=en&to=de&limit=50"
                      ).json()["rows"]
    assert all(r["sense_id"] != sid for r in rows)


def test_mehrfachuebersetzungen_pro_sinn():
    """Bis zu 3 Zielwoerter pro Zielsprache, Dubletten weiterhin 409."""
    eid = _eintrag("Mehrtest", "de", "x")
    sid = _sinn(eid, "Sinn")
    ziele = [_eintrag(f"multi{i}", "en", "x") for i in range(3)]
    for z in ziele:
        r = client.post(f"/senses/{sid}/translations",
                        json={"entry_id": z}, headers=KEY)
        assert r.status_code == 201, r.text
    # 4. Verbindung gleicher Sprache: 400 (Limit) statt Duplikat
    viertes = _eintrag("multi3", "en", "x")
    r = client.post(f"/senses/{sid}/translations",
                    json={"entry_id": viertes}, headers=KEY)
    assert r.status_code in (400, 409)


def test_sinn_loeschen_kaskadiert():
    eid = _eintrag("Kaskade", "de", "x")
    sid = _sinn(eid, "Sinn")
    client.put(f"/senses/{sid}/definitions",
               json={"lang": "de", "text": "Beschreibung"}, headers=KEY)
    r = client.delete(f"/senses/{sid}", headers=KEY)
    assert r.status_code == 204
    s = client.get(f"/entries/{eid}/senses").json()["senses"]
    assert s == []
    assert client.delete(f"/senses/{sid}", headers=KEY).status_code == 404


def test_sense_link_nur_https():
    eid = _eintrag("Linkcheck", "de", "x")
    sid = _sinn(eid, "Sinn")
    r = client.post(f"/senses/{sid}/links",
                    json={"kind": "dwds", "url": "http://unsicher.example"},
                    headers=KEY)
    assert r.status_code == 400


def test_etym_queue_nur_unbelegte():
    eid = _eintrag("etymq", "de", "mehrdeutig")
    sid = _sinn(eid, "Sinn ohne Beleg")
    rows = client.get("/senses/etym-queue?language=de&limit=200"
                      ).json()["rows"]
    assert any(r["sense_id"] == sid for r in rows)
    client.post(f"/senses/{sid}/links",
                json={"kind": "etymologie",
                      "url": "https://de.wiktionary.org/wiki/etymq",
                      "claim": "belegt", "source": "test"}, headers=KEY)
    rows = client.get("/senses/etym-queue?language=de&limit=200"
                      ).json()["rows"]
    assert all(r["sense_id"] != sid for r in rows)
