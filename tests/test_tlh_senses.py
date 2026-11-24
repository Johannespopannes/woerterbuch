# -*- coding: utf-8 -*-
"""tlh-Sinn-Planung: Homonyme/affixe bekommen je Funktion einen Sinn."""
import json
from pathlib import Path

import tlh_senses

BASE = Path(__file__).resolve().parent.parent


def test_daten_dateien_konsistent():
    t = json.loads((BASE / "app/static/tlh-grammatik.json")
                   .read_text(encoding="utf-8"))
    h = json.loads((BASE / "app/static/tlh-homonyme.json")
                   .read_text(encoding="utf-8"))
    assert t["noun_suffixes"]["types"]["2"] and t["verb_suffixes"]
    assert len(h["woerter"]) > 400  # 473 Mehrfach-Schreibungen


def test_doppelfunktion_affix_bekommt_zwei_sinne():
    t = tlh_senses.lade_tabellen()
    f = tlh_senses.affix_funktionen(t)
    assert "Nominalsuffix (Typ 2)" in f["-pu'"]
    assert "Verbal suffix (Typ 7)" in f["-pu'"]


def test_plan_traegt_varianten_definitionen():
    t = tlh_senses.lade_tabellen()
    h = tlh_senses.lade_homonyme()
    plan = tlh_senses.sinne_planen(t, h)
    # bach: Substantiv + transitives Verb mit eigenen Definitionen
    labels = {e["label"] for e in plan["bach"]}
    assert labels == {"Substantiv", "transitives Verb"}
    defs = [e["de"] for e in plan["bach"]]
    assert all(defs) and len(set(defs)) == 2
    # -pu': zwei Funktionen, je mit eigener Definition
    pu = {e["label"]: e for e in plan["-pu'"]}
    assert pu["Nominalsuffix (Typ 2)"]["de"]
    assert pu["Verbal suffix (Typ 7)"]["de"]
    assert pu["Nominalsuffix (Typ 2)"]["de"] != pu["Verbal suffix (Typ 7)"]["de"]


def test_gleiche_wortart_mehrfach_nummeriert():
    h = {"ben": [{"pos_raw": "n:1", "de": "Vergangenheit", "en": "years ago"},
                 {"pos_raw": "n:2", "de": "veraltet für Onkel", "en": None}]}
    plan = tlh_senses.sinne_planen(tlh_senses.lade_tabellen(), h)
    labels = [e["label"] for e in plan["ben"]]
    assert labels == ["Substantiv (1)", "Substantiv (2)"]


def test_plan_deterministisch():
    t, h = tlh_senses.lade_tabellen(), tlh_senses.lade_homonyme()
    assert tlh_senses.sinne_planen(t, h) == tlh_senses.sinne_planen(t, h)
