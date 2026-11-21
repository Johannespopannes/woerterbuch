# Funktionskatalog Wörterbuch

Jede Wörterbuch-Funktion wird genau einmal implementiert (in `app/static/wb.js`)
und auf den Seiten wiederverwendet. Seiten dürfen die Darstellung anpassen
(kompakt vs. groß), nicht aber Bedeutung, Texte oder Bewertungsregeln.

## Module und Verantwortlichkeiten

| Funktion | Eingaben | Ergebnis/Zustände | Backend |
|---|---|---|---|
| `WB.sprachName/WB.sprachAdj` | Sprachcode | Anzeigename / Adjektiv („deutsch“) | statisch |
| `WB.renderFlexion` | Container, Flexion-JSON | Dauerhaft sichtbare Tabelle, mobil transponiert | — |
| `WB.grammatikKarte` | Elemente, Grammar-Objekt, Wort, Sprache | Zeile (Artikel·Genus·Wortart·Grundform) + Flexion | `GET /grammatik` (via Evidence) |
| `WB.wortVote` | entry_id, kind (`dazu\|nicht\|unsicher`) | Stimme gezählt, Zähler +1 | `POST /wort/vote` |
| `WB.bildVote` | entry_id, understood, image?, nonce?, kind? | Stimme gezählt; ohne nonce → Sitzungs-Modell, mit nonce → Einmal-Modell | `POST /wort/bildvote` bzw. `POST /raten/vote` |

## Bewertungsregeln (verbindlich)

- **Eine Bildfrage**, überall: „Versteht man das Bild?“ mit **Ja / Nein**.
  Abweichungen nur hier, nicht auf den Seiten.
- Wortfrage: „Ist ‚X‘ ein deutsches Wort?“ mit **Gehört dazu / Gehört nicht
  dazu / Unsicher**.
- Wort unbekannt ist eine Bildstimme des Typs `unknown` — zählt nie als
  schlechtes Bild.

## Offen für spätere Module

- `WB.wortKarte` (Lemma + Bedeutung + Bild, kompakt und ausführlich)
- `WB.quellen` (Evidenz- und Quellenanzeige der Prüfdetails)
- `WB.linkify` (Wortverlinkungen in Definitionen, kommt von der Startseite)

## Technik

Plain JavaScript, globaler Namespace `WB`, kein Build-Schritt. `basis.css`
trägt das Aussehen, `wb.js` trägt das Verhalten. Seiten-Skripte rufen die
Module auf und bleiben für seitenweisen Ablauf (Suche, Galerie, Tastatur)
zuständig.
