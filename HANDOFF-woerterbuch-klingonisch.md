# Übergabe: Klingonisch-Lane + Laptop-Downloads (Stand 2026-11-24 ~21:10)

## Kontext

Das Wörterbuch-Projekt (Pi: `/mnt/disk2/woerterbuch/`, SQLite `woerterbuch.db`)
soll um weitere Sprachen erweitert werden. Ein begleitender Chat auf **Schleppi**
(dieser Rechner) kümmert sich um Hardware/Downloads auf dem Laptop
`johannes-sein-l` (192.168.178.33) und um ModelHub-Lanes. Dieses Dokument
übergeben, damit der Projekt-Kimi den Stand übernehmen kann.

## 1. Klingonisch in der Wörterbuch-DB (fertig)

Es gibt **kein brauchbares Klingonisch-LLM** (HF-Suche: nichts). Stattdessen:
Regelwerk + kanonische Wörterbuchdaten aus **boQwI'** (offizielle Datenquelle
der App, Repo `De7vID/klingon-assistant-data`, DB-Version 2026.07.24b).

**Eingespielt auf dem Pi** (`johannes@raspberry5-01`):

- `woerterbuch.db` → Tabelle `entries`: **5 981 Einträge**, `language='tlh'`,
  `source='boQwI'`, `confidence=1.0`. 550 Duplikate (Homonyme, UNIQUE
  `word,language`) bewusst übersprungen.
- Tabelle `grammar`: **5 981 Einträge** mit `word_class`
  (noun 3 906, verb 1 402, phrase 557, adverb 57, exclamation 45, …).
- Definitionen im DB-Stil (`Substantiv: Erfolg`); volle boQwI'-Felder
  (engl. Definition, Notes, Beispiele, Komponenten, see_also) als JSON in der
  **`evidence`-Spalte** — die anderen Sprachen lassen das Feld leer, also
  vor Verwendung parsen.
- **Backup vor dem Import:** `/tmp/woerterbuch.db.bak.klingon` auf dem Pi.
- DB vorher: 26 358 Einträge (de 7 588, it 4 793, fr 4 592, es 4 513, en 3 457,
  ar 1 415, Quellen meist `model:mistral-small-4`); jetzt 32 339.

**Artefakte im Workspace** (`C:\Repos\modelhub.tassekaffee.com\`):
- `klingon-assistant-data/` — geklontes Daten-Repo (XML-Quellen, `book/` mit
  Grammatik, Konverter `xml2json.py`). Achtung: einzelne `sentences/*.yaml`
  mit `'` im Dateinamen lassen sich unter Windows nicht auschecken —
  Wörterbuch-XML (mem-*.xml) ist vollständig.
- `klingon-dict-raw.json` (6,4 MB, Vollversion), `klingon-dict.json`
  (4,9 MB, kompakt: entry_name, part_of_speech, definition{de,en,…}, notes,
  examples, components, see_also, synonyms).
- `gen_klingon_import.py` (roh→Import-JSON), `gen_klingon_compact.py`,
  `import_klingon_pi.py` (lief auf dem Pi; macht Backup + INSERT).

**Offen / Ideen für den Projekt-Kimi:** `evidence`-JSON auslesen für
Beispielsätze/Anmerkungen in der UI; Grammatik-Regeln aus `book/` als
weitere `grammar.flexion`-Daten nutzen; ggf. Audio generieren
(Esperanto-artige Aussprache wäre machbar).

## 2. Laptop-Downloads (läuft, Stand 21:10)

Ziel: Sprach-Modelle als **Harvest-Lanes über den Pi-ModelHub** erreichbar
machen. Downloads laufen auf `johannes-sein-l` als **geplante Tasks (SYSTEM)**,
da SSH-Prozesse mit der Session sterben. Bandbreite ~10 MB/s, curl mit `-C -`
und `--retry 10`. Logs unter `C:\Users\johan\llm\`.

- **Queue `dl-lang-models`** (`download.log`): llm-jp-4.1-32b-a3b Q4_K_M
  (21,5 GB) → Jais-3-MoVA-36B-A4B Q4_K_M (22,4 GB) → MamayLM Gemma-3-12B v1.0
  Q4_K_M (7,3 GB) → `D:\Modelle\<name>\`
- **Queue `dl-lapa`** (`download-lapa.log`): lapa-v0.1.2-instruct Q4_K_M
  (7,3 GB, ukrainisch, Studien-Sieger vor MamayLM) → `D:\Modelle\Lapa-12B\`
- **Queue `dl-lang-eu`** (`download-eu.log`, sequentiell, ~152 GB):
  T-pro-it-2.0 (ru), EXAONE-3.5-32B (ko), Yi-1.5-34B-Chat (zh),
  Turkish-LLM-32B, SiamGPT-32B (⚠️ nur Q2_K), BgGPT-Gemma-3-27B (bg),
  ALIA-40b (es), Bielik-11B-v3.1 (pl), Teuken-7B (nl), Viking-7B
  (⚠️ nur Q2_K, fi), Fantastica-7b (it).
  Skript auf Laptop: `C:\Users\johan\llm\download-lang-eu.cmd`;
  Kopien im Workspace: `download-lang-eu.cmd`, `gen_eu_cmd.py`, `dlplan.json`.

## 3. Nach Abschluss der Downloads (Plan, noch nicht ausgeführt)

1. Pro Modell `llama-server` als geplanter Task, Ports: **8083 = llm-jp,
   8084 = jais, 8085 = mamaylm, 8086 = lapa, 8087+ = EU-Modelle**. Muster:
   `C:\Users\johan\llm\start-<name>.cmd` →
   `llama-server.exe -m <gguf> --host 0.0.0.0 --port <port> -c 4096 -t 5 -np 1`
   plus `schtasks /create /tn llama-<name> /tr … /sc onstart /ru SYSTEM /f`
   und Firewall-Regel per `New-NetFirewallRule`.
   Bestehende Lanes auf dem Laptop: 8081 WizardCoder13B, 8082 gpt-oss-20b.
2. **llm-jp zuerst testen** — Tokenizer-Patch-Falle
   (`llmjp4-ugm-byte-fallback`): japanische Mehrzeiler können kaputte
   Newlines liefern; dann llama.cpp patchen.
3. ModelHub-Einträge in `models_pi.yaml` (Repo, remote = Forgejo auf Pi):
   `backend: external`, `base_url: http://192.168.178.33:<port>`,
   Provider `laptop_llm: {}` — **und `backend: external` auch im
   `upstreams:`-Item**, sonst Ollama-Healthcheck → Lane dauerhaft
   „unavailable". Referenzen: `gpt-oss-20b:laptop` (8082),
   `wizardcoder-13b:laptop` (8081).
4. Nur die Live-Datei per scp auf den Pi
   (`/opt/modelhub/modelhub/dev/current/models_pi.yaml`) + `sudo -n
   systemctl restart modelhub.service`. **Nicht `deploy_pi.sh`** — im
   Repo-Working-Tree liegt eine fremde, uncommittete Änderung an
   `modelhub.py` (MODELHUB_CLIENT_KEYS), die nicht mitgeschleppt werden darf.
5. End-to-End: `curl https://modelhub.tassekaffee.com/v1/models` (Key aus
   `C:\Users\johan\.secrets\aider-modelhub.env`), dann Chat-Test. Fehler
   „worker queue exhausted" mit leeren reasons → upstreams-Backend prüfen.

## 4. Benchmarks Laptop (i7-8850H 6c/12t, 64 GB RAM, CPU-only)

- Dichtes 13B ≈ 3,7 t/s (-t 10); MoE ~4B aktiv ≈ 10–11 t/s (-t 5);
  dichte 27–40B ≈ 1–2,5 t/s erwartet. RAM reicht für ~2 geladene Modelle
  gleichzeitig → Lanes sind Einzel-Server, parallele Abfragen vermeiden.
- llama.cpp CPU b7406: `D:\Modelle\llama-b7406-bin-win-cpu-x64\llama-server.exe`
- Hilfsmittel: `gguf_info.py` im Workspace (GGUF-Header-Parser).

## Stolperfallen (gern gemacht)

- Bash-Tool frisst `\U` als Unicode-Escape → Pfade mit `C:\Users` in
  `python -c`/printf immer in Skriptdateien (Write-Tool) oder Heredoc auslagern.
- CRLF-Dateien: Edit-Tool scheitert an `\r` → Python mit `chr(13)+chr(10)`.
- Windows-Konsole: `PYTHONUTF8=1` setzen, sonst cp1252-DecodeError.
- Git-Bash-Redirect `> ..\datei.json` erzeugt versteckte Datei `..datei.json`
  im CWD (Backslash frisst es).
- SSH zum Laptop: User `johan`, Admin-Key in
  `C:\ProgramData\ssh\administrators_authorized_keys`, HostAlias
  `johannes-sein-l` in `~/.ssh/config` (Schleppi).
