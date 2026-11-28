# Fachmodell – woerterbuch.tassekaffee.com

**Status:** Entwurf zur Abnahme. Kein Datenbankschema, keine API.
Erst nach fachlicher Freigabe werden daraus Schema, Migrationen,
Endpunkte und Pipeline-Schritte abgeleitet.

**Verbindlicher Grundsatz:** Neue sprachliche Erkenntnisse führen zu
neuen Daten, nicht zu neuem Programmcode. Wortarten, grammatische
Merkmale, Flexionsformen, Schriften, Lesarten und Sprachvarianten sind
Daten. Das Deutsche ist nicht die implizite Vorlage; das Modell muss
für beliebige Sprachen tragen.

---

## 1. Die drei Identitäten

### 1.1 Schreibweise (Wortform)

Eine Schriftfolge in einer Sprache und Schrift. Trägt **keine**
Grammatik und **keine** Semantik. Dient Suche und Blättern.

- „See", „Band", „Gerichte" sind je genau eine Wortform.
- Eine Wortform kann mehreren Lexemen angehören (Homographie).
- Attribute: Sprache, Zeichenfolge, Schrift (z. B. Arabisch / Franco).

### 1.2 Lexem (grammatische Identität)

Träger von Wortart, grammatischen Merkmalen (generisch, z. B. Genus —
nicht sprachgebunden hartkodiert) und Flexionsparadigma. Zusätzlich:
Herkunft/Etymologie als Entwicklungsgeschichte des Lexems.

**Das Lexem besitzt eine eigenständige, technisch nicht abgeleitete ID.**
Die Frage „eine ID oder zwei?" ist eine fachliche Entscheidung
(Homonymie vs. Polysemie), keine Constraint-Entscheidung:

- `der Kiefer` ≠ `die Kiefer`: gleiche Schreibweise, **andere**
  Grammatik → zweifelsfrei zwei Lexeme.
- `der Hahn` (Tier) vs. `der Hahn` (Absperrvorrichtung): gleiche
  Schreibweise, **gleiche Grammatik** → zwei Lexeme, weil unterschiedliche
  Herkunft; die Entscheidung trifft die Fachseite und wird als Daten
  festgehalten, nie durch einen Unique-Constraint.
- `das Gericht` (Speise) vs. `das Gericht` (Justizorgan): wie Hahn.
- Zehn Bedeutungen mit gleicher Aussprache gehören zu **einem** Lexem,
  wenn sie fachlich als Polysemie gelten.

**Konsequenz:** Es gibt keinen „grammatischen Signaturschlüssel".
Grammatische Eigenschaften gehören Lexemen (Wortart, inhärentes Genus),
Wortformen im grammatischen Kontext (Numerus, Kasus, Tempus) und den
Beziehungen zwischen beiden (Flexionsparadigma). Dieselbe Wortform kann
mehrere grammatische Funktionen erfüllen. Merkmale unterscheiden
Wortformen voneinander, nicht Lexeme voneinander — die Lexem-Identität
ist eigenständig.

### 1.3 Bedeutung (Sinn)

Träger von Definition (je verfügbarer Sprache), Beispielsätzen,
Synonymen, Bildern, Belegen/Quellen. Gehört zu genau einem Lexem.

---

## 2. Zuordnung der Informationen (verbindlich)

| Information   | Gehört zu                                    |
|---------------|----------------------------------------------|
| Schreibweise  | Wortform                                     |
| Aussprache    | **Lesart** = (Wortform, Lexem) — mehrere Aussprachen pro Lesart möglich (z. B. regional) |
| Audio         | Lesart × Sprecher (eine Aussprache, mehrere Sprecher; geteilt von allen Bedeutungen der Lesart) |
| Wortart       | Lexem                                        |
| Inhärentes Genus | Lexem                                   |
| Numerus/Kasus/Tempus | Wortform im grammatischen Kontext   |
| Gramm. Merkmal| Lexem (generisch, als Daten je Sprache)      |
| Flexion       | Lexem + zugehörige Wortformen (Paradigma)    |
| Definition    | Bedeutung                                    |
| Bild          | Bedeutung                                    |
| Beispielsatz  | Bedeutung + die darin verwendete Wortform    |
| Übersetzung   | **Beziehung zwischen zwei Bedeutungen** (keine künstliche Obergrenze) |
| Synonym       | Beziehung zwischen zwei Bedeutungen (eine Sprache) |
| Etymologie    | Lexem bzw. dessen Entwicklungsgeschichte     |

**Lesart** löst beide Richtungen des Audio-Problems:
„umfahren" (umschreibbar vs. umfahrend) = eine Wortform, zwei Lexeme,
zwei Lesarten mit je eigener Aussprache und Audiodatei.
Zehn Bedeutungen gleicher Aussprache teilen eine Lesart und eine Datei.
Innerhalb desselben Lexems kann dieselbe Schreibweise regional
unterschiedlich ausgesprochen werden — eine Wortform-Lexem-Zuordnung
besitzt deshalb mehrere Aussprachen, jede wiederum durch mehrere
Sprecher vertont.

**Übersetzung** verbindet Bedeutung mit Bedeutung (fremdsprachig),
nicht Bedeutung mit Worteintrag. Das ist das zentrale Verbindungsglied
eines Weltsprachwörterbuchs. Die tatsächliche sprachliche Vielfalt
bestimmt die Anzahl der Beziehungen — eine willkürliche technische
Obergrenze (bisher max. 3 je Zielsprache) entfällt.

**Mehrwort-Lexeme** (Wortgruppen, Phrasen) werden regulär unterstützt:
Leerzeichen sind kein Grund für eine andere Architektur.

**Sprachen ohne Genus oder Flexion:** entsprechende Eigenschaften sind
nicht vorhanden bzw. nicht anwendbar. Das Modell erzwingt sie nicht —
grammatische Merkmale sind optional und datengetrieben.

---

## 3. Beziehungen (konzeptionell)

```
Wortform 1 ──< Lesart >── n Lexem 1 ──< Bedeutung
   n                    (Aussprache,
   |                     Audio)
   └── Lexem 2 ──< Bedeutung

Lexem ──< Paradigmazelle >── Wortform        (Flexion)
Bedeutung A (Sprache X) ──< Übersetzung >── Bedeutung B (Sprache Y)
Bedeutung ──< Beispielsatz >── Wortform
Bedeutung ──< Bild / Definition / Beleg / Synonyme
Lexem ──< Etymologie (Entwicklungsgeschichte)
```

Eigenständige Entitäten mit ID: Wortform, Lesart, Lexem, Bedeutung,
Übersetzungsbeziehung, Beispielsatz. Attribute der anderen: Paradigma-
zelle, Definition, Bild (Referenz auf Datei + Beziehung zur Bedeutung),
Beleg, Etymologieseintrag.

---

## 4. Bestand und Migration (keine Legitimation durch Übernahme)

Die 60.565 vorhandenen Einträge sind **gesammelte Schreibweisen mit
unterschiedlich vollständigen und unterschiedlich zuverlässigen
Informationen**. Sie werden vollständig erhalten.

Sie werden **nicht** pauschal zu Lexemen: „Gerichte", „Gerichten",
„Gericht", „Gerichts" sind nicht automatisch vier Lexeme.

Migrations- und Produktionsregel:

1. Bestand geht als Wortformen mit seinen bisherigen Informationen
   (Definitionstexte, Bilder, Stimmen, Herkunftsangaben) in das neue
   Modell ein — roh, mit Herkunftsnachweis, unbestätigt. Wortebenen-
   Informationen, die keiner Bedeutung zugeordnet werden können,
   bleiben als solche sichtbar (unzugeordneter Bestand), statt durch
   eine erfundene Zuordnung bestätigt zu wirken.
2. Die **Zuordnung von Wortformen zu Lexemen und von Informationen zu
   Bedeutungen ist ausschließlich Aufgabe der Produktionspipeline**,
   die eigenständig entscheidet: sie sammelt sprachliche Informationen
   und sprachliche Belege, ergänzt vorhandene Informationen, erkennt
   Zusammenhänge, untersucht Widersprüche, trifft Entscheidungen und
   dokumentiert sie — und korrigiert frühere Entscheidungen bei neuen
   Erkenntnissen.
3. **Unsicherheit darf bestehen bleiben.** Sie wird nicht durch eine
   erfundene eindeutige Zuordnung beseitigt. Unbestätigte Zuordnungen
   sind als solche markiert und bleiben als Daten erhalten. Eine
   obligatorische menschliche Einzelfreigabe (bei 60.565+ Einträgen
   und Weltsprachziel) gibt es nicht; das Wörterbuch soll sich autonom
   entwickeln.

---

## 5. Verantwortlichkeiten (Trennung System / Inhalt)

| Aufgabe                                   | Wer                    |
|-------------------------------------------|------------------------|
| System entwickeln (Code, Schema, Pipeline-Mechanik) | Kimi (System) |
| Sprachliche Inhalte sammeln, zuordnen, prüfen, veröffentlichen, Entscheidungen treffen und dokumentieren | Produktionspipeline |
| Fachliche Regeln (Homonymie-Kriterien, Prüfschwellen) | konfigurierbare fachliche Regeln — Daten, kein Code pro Sprache |
| Direkte Content-Eingriffe außerhalb definierter Pipeline-Schritte | **nicht vorgesehen** |

Entscheidungen der Pipeline (z. B. Homonymie-Fälle wie „der Hahn")
werden **als Daten** festgehalten: Entscheidung, Belege, Modell-
bewertung, Zeitpunkt, Herkunft. Konfigurierbare fachliche Regeln
bestimmen, wie die Pipeline entscheidet; die konkreten Entscheidungen
und ihre Belege sind Daten. Neue sprachliche Erkenntnisse führen zu
neuen Daten, nicht zu neuem Programmcode.

Sprachliche Hinweise (z. B. „das Gerichte" als mögliche Bedeutung)
werden **Untersuchungskandidaten** für die Pipeline, nicht eingetragen
und nicht verworfen. Die Pipeline prüft Verbreitung und Belege; das
Ergebnis landet mit Herkunftsnachweis und Unsicherheitsmarkierung im
Bestand.

Jede Information trägt weiterhin Herkunft (`source`) und — wo möglich —
einen sprachlichen Beleg. `source` dokumentiert erzeugenden Prozess,
Belege dokumentieren die Sprachwirklichkeit; beides ist gefordert.

---

## 6. Widerspruchsfreiheit (Selbstprüfung gegen die bekannten Fälle)

| Fall                              | Auflösung im Modell |
|-----------------------------------|---------------------|
| der See / die See                 | 1 Wortform, 2 Lexeme (Grammatik differiert) |
| das/der/die Band                  | 1 Wortform, 3 Lexeme |
| der Hahn × 2                      | 1 Wortform, 2 Lexeme, gleiche Grammatik, Pipeline-Entscheidung mit Belegen |
| das Gericht × 2                   | wie Hahn |
| das Gerichte (Substantivierung)   | eigener Untersuchungskandidat; erst Pipeline-Prüfung, dann ggf. eigenes Lexem |
| umfahren (2 Aussprachen)          | 1 Wortform, 2 Lexeme, 2 Lesarten, 2 Audios |
| regionale Aussprachevariante      | 1 Lesart, mehrere Aussprachen, je mehrere Sprecher |
| 10 Bedeutungen, 1 Aussprache      | 1 Lesart, 1 Audio, geteilt |
| Gerichte/Gerichten/Gericht/Gerichts | Wortformen; Lexem-Zuordnung nur per Pipeline |
| Mehrwort-Lexem („zum Teufel")     | reguläres Lexem, keine Sonderarchitektur |
| Sprache ohne Genus/Flexion        | Merkmale schlicht nicht vorhanden — das Modell erzwingt sie nicht |

**Status:** abgenommenes Fachmodell (Bestandteil des Repos,
`docs/fachmodell.md`). Änderungen nur noch auf fachlicher Ebene, nicht
als Code-Sonderfall.
