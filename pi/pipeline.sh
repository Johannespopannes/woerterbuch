#!/bin/bash
# Wörterbuch-Pipeline — läuft KOMPLETT allein auf dem Pi (raspberry5-01).
#
# Kein Laptop nötig: alle LLM-Aufrufe gehen über die ModelHub-Cloud
# (Text: mistral-large-4/qwen3.8-max, Vision: qwen3.8-flash), die Bilder
# rendert Wan in der Cloud. Die API wird lokal auf 127.0.0.1:8805 angesprocen.
#
# Zyklus-Design (Round-Robin, seit 23.11.): JEDER Zyklus ist abschließend
# begrenzt — genau eine Textsprache, ein Übersetzungsziel, eine Bildsprache.
# Davor liefen alle 9 Sprachketten je Zyklus; der langsame suggest-Schritt
# fraß den Zyklus, und define/Sinne/Quellen/Übersetzungs-Rotation/Bilder
# kamen faktisch nie dran. So bekommt jeder Schritt jeden Zyklus seinen
# garantierten Slot, und ein Abbruch kostet maximal einen begrenzten Zyklus.
#
# Idempotent: Seeds deduplizieren, define überspringt definierte Einträge,
# die API antwortet auf Dubletten mit 409, Queues liefern nur offene Arbeit.
# Log: journalctl -u woerterbuch-pipeline -f
set -u
cd "$(dirname "$0")/.."   # /home/johannes/woerterbuch

export DICT_API_URL=http://127.0.0.1:8805
export DICT_API_KEY="VHixW4pMVFCX_aaVD6TwTsbECZkqDPcM"
export MODEL_BASE_URL=https://modelhub.tassekaffee.com/v1 MODEL_API_KEY=dummy
export WB_RENDER_CLOUD=1
export WB_PICK_MODEL=qwen3.8-flash

MISTRAL=mistral-large-4:cloud
QWEN=qwen3.8-max
LANGS_TXT=(de en es fr it ru zh ja hi)
LANGS_IMG=(de en es fr it ru zh ja hi ar tn tlh)

# --- Harvest-Lanes über den ModelHub (Sprachspezialisten auf dem
# Laptop johannes-sein-l) — Regeln, sobald sie existieren:
#   * NIE zwei Lanes parallel belasten: „verfügbar" != „parallel nutzbar"
#     (12 Threads geteilt → beide kriechen). Unser Zyklus ist ohnehin
#     streng sequentiell — so lassen.
#   * Durchsatz dichte 32B auf CPU: 1–2,5 t/s → 7–12B-Lanes zuerst
#     (über Nacht sinnvoll), 32B+ nur kleine Batches (per-round/batch
#     halbieren) und MODEL_TIMEOUT=900 setzen.
#   * Modell je Sprache hier in modell_fuer() eintragen, wenn die Lane
#     im ModelHub live ist.

log()  { echo; echo "======== $(date '+%d.%m. %H:%M:%S')  $1 ========"; }
run()  { echo "---- $(date '+%H:%M:%S') START: $*"; python -u "$@" || echo "!! $* (rc=$?)"; echo "---- $(date '+%H:%M:%S') ENDE: $1 $2 $3"; }

modell_fuer() {
  case "$1" in
    zh|ja|hi|ru) echo "$QWEN" ;;
    *)           echo "$MISTRAL" ;;
  esac
}

# Sprachkette: Verarbeitung ZUERST (garantiert dran), Ernte danach.
# Fällt der Zyklus mitten in suggest/gap aus, ist die Verarbeitung der
# Sprache trotzdem komplett gelaufen.
kette() {
  local lang=$1
  export MODEL_NAME=$(modell_fuer "$lang")
  log "Kette $lang via $MODEL_NAME"
  run harvester.py --seed --language "$lang"
  run harvester.py --mode define  --language "$lang" --per-round 30 --delay 0.3
  run define_langs.py --mode own --language "$lang" --limit 300 --batch 20 --delay 0.4
  run sense_split.py --language "$lang" --limit 150 --batch 5
  run sense_etym.py --language "$lang" --limit 80 --batch 5
  run source_links.py --language "$lang" --limit 400
  run audit_eu.py --language "$lang"
  run harvester.py --mode suggest --language "$lang" --letters all --rounds 1 --per-round 60 --delay 0.5
  run harvester.py --mode gap     --language "$lang" --letters all --rounds 1 --per-round 15 --pairs 4 --delay 0.5
}

zyklen=0
while true; do
  zyklen=$((zyklen+1))
  log "ZYKLUS $zyklen startet"
  idx=$(( (zyklen - 1) % ${#LANGS_TXT[@]} ))

  # --- 1 Textsprache je Zyklus (Round-Robin über alle 9) ---
  kette "${LANGS_TXT[$idx]}"

  # --- ar/tn: Ernte/Audit/Franco laufen auf dem LAPTOP via Jais (Sonder-
  # modelle, staerkste Arabisch-Qualitaet) — siehe ar-tn-loop.sh/franco-loop.sh.
  # Der Pi pflegt hier nur die ar-Quellenlinks (ar-Wiki existiert; tn hat
  # kein eigenes Wiki und wird deshalb uebersprungen).
  run source_links.py --language ar --limit 300

  # --- tlh: kanonische Sinne aus den boQwI'-Tabellen/Homonymen (ohne LLM,
  # idempotent — legt je grammatischer Funktion einen Sinn an) ---
  run tlh_senses.py

  # --- Bedeutungs-Matrix: je Zyklus ein Ziel rotieren ---
  T=${LANGS_IMG[$(( (zyklen - 1) % ${#LANGS_IMG[@]} ))]}
  export MODEL_NAME=$(modell_fuer "$T")
  log "Bedeutungs-Übersetzung -> $T via $MODEL_NAME"
  run define_langs.py --mode translate --target "$T" --limit 400 --batch 15 --delay 0.5

  # --- Sinn-Ebene: Übersetzungsverbindungen bedeutungsgerecht über die
  # Sinne (Bank=Finanzinstitut -> bank, Bank=Sitzmöbel -> bench) ---
  log "Sinn-Übersetzung de -> $T"
  run sense_translate.py --from de --to "$T" --limit 100

  # --- Sinn-Beschreibungs-Matrix: jede Bedeutung zusätzlich in $T
  # erklärt (an den Sinn gebunden, nicht nur am Wort) ---
  log "Sinnbeschreibungen de -> $T"
  run sense_def_translate.py --from de --to "$T" --limit 120

  # --- Definitions-Sprachaudit: pauschal als 'de' übernommene Legacy-
  # Texte per Modell prüfen, falsch zugeordnete in die richtige Sprache
  # verschieben (entries.definition-Cache wird dabei sauber frei) ---
  log "Definitions-Sprachaudit (Legacy-de)"
  run fix_def_lang.py --limit 100 --batch 10 --model "$MISTRAL"

  # --- Bilder: eine Sprache je Zyklus (Wan-Cloudrender, Vision-Pick) ---
  I=${LANGS_IMG[$(( (zyklen - 1) % ${#LANGS_IMG[@]} ))]}
  log "Bilder-Runde $I"
  run image_harvester.py --language "$I" --limit 60

  log "ZYKLUS $zyklen fertig — Pause 5 min"
  sleep 300
done
