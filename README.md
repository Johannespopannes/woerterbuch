# Wörterbuch API + LLM-Harvester

Eigenes Wörterbuch mit Schreib-/Lese-API. Der Harvester fragt ein
OpenAI-kompatibles Modell systematisch nach Wörtern (Alphabet-Loop) und
füllt die Datenbank.

## Start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # läuft auf http://127.0.0.1:8000
# Swagger-UI: http://127.0.0.1:8000/docs
```

Die SQLite-Datei `woerterbuch.db` entsteht beim ersten Start im Projektordner.

## Endpunkte

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/entries` | Einzelnen Eintrag anlegen (409 bei Duplikat) |
| POST | `/entries/batch` | Viele Einträge, Duplikate werden übersprungen |
| GET | `/entries` | Liste, Filter: `language`, `prefix`, `q`, `limit`, `offset` |
| GET | `/lookup?word=…&language=…` | Exaktes Nachschlagen |
| GET | `/languages` | Sprachen mit Anzahl Einträge |
| GET | `/stats` | Gesamtanzahl |
| DELETE | `/entries/{id}` | Eintrag löschen |

Eintrag: `{word, language, definition?, source?}` — `source` markiert die
Herkunft, der Harvester schreibt z. B. `model:kolibri-1`.

## Sicherheit & Schutz vor Missbrauch

Für eine öffentlich erreichbare Demo:

- **Lesen ist offen, Schreiben braucht einen Key:** POST/DELETE-Endpunkte
  verlangen den Header `X-API-Key`, der per Umgebungsvariable `API_KEY`
  gesetzt wird. Ist keiner gesetzt, wird beim Start ein zufälliger Key
  generiert und einmalig ins Log geschrieben — nie unbeabsichtigt offen.
- **Rate-Limiting** (eigene Middleware, pro IP): 60 Lesezugriffe/Minute,
  5 Schreibzugriffe/Minute. Mit gültigem Key: 600 Schreibzugriffe/Minute —
  so kann der Harvester ungestört arbeiten, Bots ohne Key bleiben eng.
  Grenzen per Umgebungsvariable anpassbar: `RATE_READ_PER_MIN`,
  `RATE_WRITE_PER_MIN`, `RATE_WRITE_KEYED_PER_MIN`. Zähler laufen
  pro Prozess (bei mehreren Workern nicht global).
- **Payload-Grenzen:** Wort max. 100 Zeichen, Definition max. 1000,
  Batch max. 500 Einträge, Sprache nur Kleinbuchstaben.

```bash
API_KEY=mein-geheimer-key uvicorn app.main:app --reload
curl -X POST http://127.0.0.1:8000/entries \
  -H 'Content-Type: application/json' -H 'X-API-Key: mein-geheimer-key' \
  -d '{"word":"Apfel","language":"de"}'
```

Der Harvester setzt den Key aus der Umgebungsvariable `DICT_API_KEY`
automatisch mit.

## Harvester

Baut nur auf der Python-Standardbibliothek, braucht kein openai-Paket.
Jeder OpenAI-kompatible Server geht: Ollama, vLLM, LM Studio, dein
Home-Server — oder api.openai.com.

```bash
export MODEL_BASE_URL=http://home-server:11434/v1   # Ollama-Beispiel
export MODEL_API_KEY=dummy                          # bei Ollama egal
export MODEL_NAME=kolibri-1
export DICT_API_URL=http://127.0.0.1:8000
export DICT_API_KEY=mein-geheimer-key
```

### Drei Strategien

**1. Startbestand einspielen** (eingebaute Seeds, de/en):

```bash
python3 harvester.py --seed --language de
```

**2. `suggest` — Vorschläge zum Erweitern:** Dem Modell bis zu 10 bekannte
Wörter eines Buchstaben zeigen, es darf weitere vorschlagen. Läuft, bis
eine Runde nichts Neues liefert.

```bash
python3 harvester.py --mode suggest --language de --letters all --rounds 5
```

**3. `gap` — Lücken schließen:** Zwei alphabetische Nachbarn aus der DB
nehmen und gezielt fragen, welche Wörter dazwischen liegen. Läuft, bis
eine Runde keine Lücke mehr füllt. Modelle sortieren zuverlässiger, als
sie frei aufzählen — das ist der effektivste Modus.

```bash
python3 harvester.py --mode gap --language de --letters all --pairs 4
```

Weitere Optionen: `--rounds`, `--per-round`, `--temperature`, `--delay`,
`--letters a-z|a-f|all|ä,ö,ü,ß`. Alle Modus-Loops deduplizieren lokal und
gegen die API, importieren gebatcht und setzen nach Abbruch nahtlos fort.

## Gedanke

Ein LLM kann sein Vokabular nicht vollständig aufzählen. Die Sequenz
seeds → suggest → gap nähert sich aber trotzdem gut an: erst Anker setzen,
dann erweitern, dann die sortierten Lücken gezielt füllen. Für ein echtes
Vollwörterbuch lässt sich später z. B. FreeDict/Wiktionary als
zusätzliche Quelle an denselben Endpunkten importieren.
