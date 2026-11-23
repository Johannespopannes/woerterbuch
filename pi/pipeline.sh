#!/bin/bash
# Wörterbuch-Pipeline — läuft KOMPLETT allein auf dem Pi (raspberry5-01).
#
# Kein Laptop nötig: alle LLM-Aufrufe gehen über die ModelHub-Cloud
# (Text: mistral-large-4/qwen3.8-max, Vision: qwen3.8-flash), die Bilder
# rendert Wan in der Cloud. Die API wird lokal auf 127.0.0.1:8000 angesprocen.
#
# Endloser Zyklus, idempotent: Seeds deduplizieren, define überspringt
# definierte Einträge, die API antwortet auf Dubletten mit 409.
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
LANGS_IMG="de en es fr it ru zh ja hi ar tn"

log()  { echo; echo "======== $(date '+%d.%m. %H:%M:%S')  $1 ========"; }
run()  { echo "---- $(date '+%H:%M:%S') START: $*"; python -u "$@" || echo "!! $* (rc=$?)"; echo "---- $(date '+%H:%M:%S') ENDE: $1 $2 $3"; }

# Sprachkette: $1=Sprache $2=Modell $3=Audit-Skript
kette() {
  local lang=$1 model=$2 audit=${3:-audit_eu.py}
  export MODEL_NAME=$model
  log "Kette $lang via $model"
  run harvester.py --seed --language "$lang"
  run harvester.py --mode suggest --language "$lang" --letters all --rounds 1 --per-round 60 --delay 0.5
  run harvester.py --mode gap     --language "$lang" --letters all --rounds 1 --per-round 15 --pairs 4 --delay 0.5
  run harvester.py --mode define  --language "$lang" --per-round 30 --delay 0.3
  run define_langs.py --mode own --language "$lang" --limit 300 --batch 20 --delay 0.4
  run sense_split.py --language "$lang" --limit 150 --batch 5
  run sense_etym.py --language "$lang" --limit 80 --batch 5
  run source_links.py --language "$lang" --limit 400
  run "$audit" --language "$lang"
}

zyklen=0
while true; do
  zyklen=$((zyklen+1))
  log "ZYKLUS $zyklen startet"

  # --- Europäisch ---
  for L in de en es fr it; do kette "$L" "$MISTRAL" audit_eu.py; done

  # --- zh/ja/hi/ru ---
  for L in zh ja hi ru; do kette "$L" "$QWEN" audit_eu.py; done

  # --- ar/tn: Ernte/Audit/Franco laufen auf dem LAPTOP via Jais (Sonder-
  # modelle, staerkste Arabisch-Qualitaet) — siehe ar-tn-loop.sh/franco-loop.sh.
  # Der Pi rendert hier nur die Bilder (Wan-Cloudrender) und uebersetzt
  # ar/tn ueber die Cloud (Ziel-Rotation unten).

  # --- Externquellen-Links fuer ar (ar-Wiki existiert; tn hat kein
  # eigenes Wiki und wird deshalb uebersprungen) ---
  run source_links.py --language ar --limit 300

  # --- Bedeutungs-Matrix: je Zyklus ein Ziel rotieren (de+en zuerst) ---
  TARGETS=(de en it fr es ru zh ja hi ar tn)
  T=${TARGETS[$(( (zyklen - 1) % ${#TARGETS[@]} ))]}
  case "$T" in
    zh|ja|hi|ru) export MODEL_NAME=$QWEN ;;
    *)           export MODEL_NAME=$MISTRAL ;;
  esac
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

  # --- Bilder (Wan-Cloudrender, Vision-Pick via ModelHub) ---
  log "Bilder-Runde"
  for L in $LANGS_IMG; do
    run image_harvester.py --language "$L" --limit 40
    sleep 10
  done

  log "ZYKLUS $zyklen fertig — Pause 10 min"
  sleep 600
done
