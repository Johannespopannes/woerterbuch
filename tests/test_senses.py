"""Tests fuer die Sinn-Ebene (Wort -> Bedeutung -> Uebersetzung)."""
import os
import tempfile
from pathlib import Path

os.environ["RATE_WRITE_PER_MIN"] = "10000"
os.environ["RATE_WRITE_KEYED_PER_MIN"] = "10000"
os.environ["RATE_VOTE_PER_MIN"] = "10000"
os.environ["API_KEY"] = "test-key-senses"
os.environ["DICT_API_KEY"] = "test-key-senses"

import app.db as db
from app.main import app, API_KEY
import sense_translate

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
    # Fachmodell: keine Obergrenze — der Sinn hat eine EN-Verbindung und
    # ist damit raus aus der Queue (weitere Verbindungen bleiben möglich)
    rows = client.get("/senses/translate-queue?from=de&to=en&limit=50"
                      ).json()["rows"]
    assert all(r["sense_id"] != s1 for r in rows)
    # zweite Verbindung zu einem anderen EN-Wort bleibt erlaubt (201)
    corp = _eintrag("bankcorp", "en", "x")
    r = client.post(f"/senses/{s1}/translations",
                    json={"entry_id": corp}, headers=KEY)
    assert r.status_code == 201, r.text


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
    """Keine Obergrenze: beliebig viele Zielwoerter pro Zielsprache,
    Dubletten (gleiche Ziel-Bedeutung) weiterhin 409."""
    eid = _eintrag("Mehrtest", "de", "x")
    sid = _sinn(eid, "Sinn")
    ziele = [_eintrag(f"multi{i}", "en", "x") for i in range(4)]
    for z in ziele:
        r = client.post(f"/senses/{sid}/translations",
                        json={"entry_id": z}, headers=KEY)
        assert r.status_code == 201, r.text
    # erneute Verbindung zum selben Ziel -> 409 (Duplikat)
    r = client.post(f"/senses/{sid}/translations",
                    json={"entry_id": ziele[0]}, headers=KEY)
    assert r.status_code == 409


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


def test_sinn_bilder_zuordnen():
    """Bilder sind eintragsweit, können aber einem Sinn zugeordnet werden."""
    import base64
    from app.main import IMAGES_DIR
    eid = _eintrag("Gerichtetest", "de", "mehrdeutig")
    s1 = _sinn(eid, "Mahlzeit")
    s2 = _sinn(eid, "juristisches Organ")
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    fn = "gerichtetest.png"
    r = client.post(f"/entries/{eid}/image",
                    json={"filename": fn, "data_b64": png}, headers=KEY)
    assert r.status_code == 200, r.text
    try:
        # Zuordnung zum zweiten Sinn — erscheint nur dort
        r = client.post(f"/senses/{s2}/images",
                        json={"filename": fn}, headers=KEY)
        assert r.status_code == 200, r.text
        s = client.get(f"/entries/{eid}/senses").json()["senses"]
        assert next(x for x in s if x["id"] == s2)["images"] == [fn]
        assert next(x for x in s if x["id"] == s1)["images"] == []
        # fremde Datei des Eintrags -> 404
        r = client.post(f"/senses/{s2}/images",
                        json={"filename": "fremd.png"}, headers=KEY)
        assert r.status_code == 404
        # Zuordnung aufheben
        r = client.post(f"/senses/{s2}/images",
                        json={"filename": fn, "assign": False}, headers=KEY)
        assert r.status_code == 200, r.text
        s = client.get(f"/entries/{eid}/senses").json()["senses"]
        assert next(x for x in s if x["id"] == s2)["images"] == []
    finally:
        (IMAGES_DIR / fn).unlink(missing_ok=True)


def test_uebersetzungsziel_wird_semantisch_gewaehlt(monkeypatch):
    """Gleiche Wortart darf Speise und Justizorgan nicht verwechseln."""
    monkeypatch.setattr(
        sense_translate, "api_get",
        lambda _path: {"senses": [
            {"id": 10, "label": "Mahlzeit", "word_class": "Substantiv",
             "definitions": {"de": "zubereitete Speise"}},
            {"id": 11, "label": "Justizorgan", "word_class": "Substantiv",
             "definitions": {"de": "staatliche Rechtsprechungsinstanz"}},
        ]})
    monkeypatch.setattr(
        sense_translate, "post_chat_completions",
        lambda _body, _timeout: {
            "choices": [{"message": {"content": '{"id":11}'}}]})
    quelle = {"word": "Gericht", "label": "Justizbehörde",
              "text": "staatliche Einrichtung, die Recht spricht",
              "word_class": "Substantiv"}
    assert sense_translate.sinn_einordnen(quelle, 7, "testmodell") == 11


def test_uebersetzungsziel_bleibt_bei_unklarheit_offen(monkeypatch):
    monkeypatch.setattr(
        sense_translate, "api_get",
        lambda _path: {"senses": [
            {"id": 20, "label": "A", "definitions": {}},
            {"id": 21, "label": "B", "definitions": {}},
        ]})
    monkeypatch.setattr(
        sense_translate, "post_chat_completions",
        lambda _body, _timeout: {
            "choices": [{"message": {"content": '{"id":null}'}}]})
    assert sense_translate.sinn_einordnen(
        {"word": "x", "label": "unklar", "text": ""}, 8, "testmodell"
    ) is None


def test_vorlaeufiger_sinn_wird_regulaer_geprueft_und_erhalten():
    eid = _eintrag("Pruefsinn", "de", "Altbestand")
    sid = _sinn(eid, "unbelegte Behauptung")
    queue = client.get(
        "/senses/review-queue", params={"language": "de", "limit": 200}
    ).json()["rows"]
    assert any(row["sense_id"] == sid for row in queue)
    r = client.patch(
        f"/senses/{sid}/review",
        json={"valid": False, "source": "sense-review:test"},
        headers=KEY)
    assert r.status_code == 200, r.text
    assert client.get(f"/entries/{eid}/senses").json()["senses"] == []
    conn = db.get_conn()
    try:
        saved = conn.execute(
            "SELECT status FROM meanings WHERE id = ?", (sid,)).fetchone()
        assert saved["status"] == "rejected"
    finally:
        conn.close()


def test_bildqueue_arbeitet_auf_geprueften_bedeutungen():
    eid = _eintrag("Bildsinn", "de", "Wortebenenbestand")
    sid = _sinn(eid, "konkreter Sinn")
    client.put(
        f"/senses/{sid}/definitions",
        json={"lang": "de", "text": "ein konkret sichtbarer Gegenstand"},
        headers=KEY)
    client.patch(
        f"/senses/{sid}/review",
        json={"valid": True, "label": "Gegenstand",
              "word_class": "Substantiv",
              "description": "ein konkret sichtbarer Gegenstand"},
        headers=KEY)
    rows = client.get(
        "/senses/image-queue", params={"language": "de", "limit": 500}
    ).json()["rows"]
    row = next(x for x in rows if x["sense_id"] == sid)
    assert row["entry_id"] == eid
    assert row["label"] == "Gegenstand"
