# -*- coding: utf-8 -*-
"""tlh-Sinne aus boQwI'-Kanondaten — deterministisch, ohne LLM.

Löst die Mehrdeutigkeitslücke sauber auf: Schreibung bleibt EIN Eintrag
(UNIQUE(word, language) bleibt als Pipeline-Dedup-Schutz erhalten),
grammatische Funktionen werden zu SINNEN:

  * Affixe (aus tlh-grammatik.json): ein Sinn je Tabellen-Funktion,
    Label „Nominalsuffix (Typ 2)" / „Verbal suffix (Typ 7)" / „Verbpräfix" —
    die Wortseiten-Tabelle verlinkt mit &f=<Label> genau diesen Sinn.
  * Homonyme (tlh-homonyme.json, 473 Schreibungen mit >1 boQwI'-Variante,
    u. a. die beim Import verworfenen 550 Duplikate): ein Sinn je Variante
    mit deren eigener deutscher/englischer Definition.

Idempotent: vorhandene Sinn-Labels werden übersprungen. Läuft deshalb
autonom in jedem Pipeline-Zyklus.

Aufruf:
  DICT_API_URL=... DICT_API_KEY=... python tlh_senses.py
"""
import json
import os
import sys
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent

# pos_raw → deutsche Wortart (gespiegelt aus gen_klingon_live_sql.py)
POS_KURZ = [
    ("v:t_c", "transitives Verb"), ("v:t", "transitives Verb"),
    ("v:is", "intransitives Verb"), ("v:i", "intransitives Verb"),
    ("v", "Verb"),
    ("n:being", "Substantiv"), ("n:anim", "Substantiv"),
    ("n:body", "Substantiv"), ("n:place", "Substantiv"),
    ("n:name", "Substantiv"), ("n", "Substantiv"),
    ("sen:phr", "Redewendung"), ("sen", "Satz"), ("adv", "Adverb"),
    ("conj", "Konjunktion"), ("excl", "Ausruf"), ("num", "Zahlwort"),
    ("pro", "Pronomen"), ("ques", "Fragepartikel"),
    ("pref", "Präfix"), ("suff", "Suffix"), ("affix", "Affix"),
]


def wortart(pos_raw: str) -> str:
    for prefix, de in POS_KURZ:
        if pos_raw == prefix or pos_raw.startswith(prefix + ",") \
                or pos_raw.startswith(prefix + ":"):
            return de
    return "Andere"


def lade_tabellen() -> dict:
    return json.loads((BASE / "app" / "static" / "tlh-grammatik.json")
                      .read_text(encoding="utf-8"))


def lade_homonyme() -> dict:
    p = BASE / "app" / "static" / "tlh-homonyme.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))["woerter"]


def affix_funktionen(t: dict) -> dict:
    """affix -> [Label, ...] aus den Tabellen (Reihenfolge: Nomen, Verben,
    Präfix)."""
    out: dict[str, list[str]] = {}

    def add(affix: str, label: str) -> None:
        out.setdefault(affix, [])
        if label not in out[affix]:
            out[affix].append(label)

    for typ, liste in t["noun_suffixes"]["types"].items():
        for a in liste:
            add(a, f"Nominalsuffix (Typ {typ})")
    for typ, liste in t["verb_suffixes"]["types"].items():
        for a in liste:
            name = "Rover" if typ == "Rover" else f"Typ {typ}"
            add(a, f"Verbal suffix ({name})")
    for zeile in t["verb_prefixes"]["rows"] + t["verb_prefixes"]["imperativ"]:
        for z in zeile["prefixes"]:
            if z and z not in ("—",) and not z.startswith("∅") \
                    and "-" in z:
                add(z, "Verbpräfix")
    for z in t["verb_prefixes"]["unpersoenlich"]["prefixes"]:
        if z and z != "—" and not z.startswith("∅") and "-" in z:
            add(z, "Verbpräfix (unpersönlich)")
    return out


def variante_fuer(varianten: list, familienpraefix: str) -> dict | None:
    for v in varianten:
        if v["pos_raw"].startswith(familienpraefix):
            return v
    return None


def sinne_planen(t: dict, homonyme: dict) -> dict:
    """word -> [{label, de, en}] — reine Datenlogik (testbar)."""
    plan: dict[str, list[dict]] = {}
    affixe = affix_funktionen(t)

    for wort, labels in affixe.items():
        varianten = homonyme.get(wort, [])
        eintraege = []
        for label in labels:
            fam = "n" if label.startswith("Nominal") else "v"
            v = variante_fuer(varianten, fam)
            eintraege.append({
                "label": label,
                "de": (v or {}).get("de", ""),
                "en": (v or {}).get("en", ""),
            })
        plan[wort] = eintraege

    for wort, varianten in homonyme.items():
        if wort in plan:
            continue  # Affixe sind schon mit Tabellen-Labeln geplant
        klassen = [wortart(v["pos_raw"]) for v in varianten]
        eintraege = []
        zaehler: dict[str, int] = {}
        for v, k in zip(varianten, klassen):
            zaehler[k] = zaehler.get(k, 0) + 1
            label = k if klassen.count(k) == 1 \
                else f"{k} ({zaehler[k]})"
            eintraege.append({"label": label,
                              "de": v.get("de", ""), "en": v.get("en", "")})
        plan[wort] = eintraege
    return plan


def main() -> None:
    from harvester import api_get, api_post, api_put

    t = lade_tabellen()
    homonyme = lade_homonyme()
    plan = sinne_planen(t, homonyme)
    print(f"Plan: {len(plan)} Schreibungen mit Sinndefinitionen",
          flush=True)

    stat = {"sinne": 0, "definitionen": 0, "vorhanden": 0, "kein_eintrag": 0}
    for i, (wort, eintraege) in enumerate(plan.items(), 1):
        try:
            lu = api_get("/lookup?" + urllib.parse.urlencode(
                {"word": wort, "language": "tlh"}))
        except Exception:
            lu = None
        if not lu:
            stat["kein_eintrag"] += 1
            continue
        try:
            vorhanden = {s["label"] for s in api_get(
                f"/entries/{lu['id']}/senses")["senses"]}
        except Exception as e:
            print(f"  {wort}: senses lesen fehlgeschlagen: {e}",
                  file=sys.stderr)
            continue
        for e in eintraege:
            if e["label"] in vorhanden:
                stat["vorhanden"] += 1
                continue
            try:
                r = api_post(f"/entries/{lu['id']}/senses",
                             {"label": e["label"], "word_class": "",
                              "source": "boQwI'"})
                sid = r["sense_id"]
                stat["sinne"] += 1
                for lang, txt in (("de", e["de"]), ("en", e["en"])):
                    if txt:
                        api_put(f"/senses/{sid}/definitions",
                                {"lang": lang, "text": txt[:1000],
                                 "source": "boQwI'"})
                        stat["definitionen"] += 1
                vorhanden.add(e["label"])
            except Exception as ex:
                print(f"  {wort} / {e['label']}: {ex}", file=sys.stderr)
        if i % 100 == 0:
            print(f"  {i}/{len(plan)}: {stat}", flush=True)
    print(f"fertig: {stat}")


if __name__ == "__main__":
    main()
