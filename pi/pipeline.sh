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

export DICT_API_URL=http://127.0.0.1:8000
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

  # --- Arabisch/Tunesisch (Franco-Qualität von Mistral-Cloud geprüft) ---
  export MODEL_NAME=$MISTRAL
  log "Kette ar via $MISTRAL"
  run harvester.py --seed --language ar
  run harvester.py --mode free    --language ar --letters ar --rounds 2 --per-round 25 --delay 0.3
  run harvester.py --mode suggest --language ar --letters ar --rounds 1 --per-round 25 --delay 0.3
  run harvester.py --mode gap     --language ar --letters ar --rounds 1 --per-round 8  --pairs 4 --delay 0.3
  run harvester.py --mode define  --language ar --per-round 15 --delay 0.2
  run audit_ar.py --language ar
  run merge_ar_dupes.py --apply --language ar

  log "Kette tn via $MISTRAL"
  run harvester.py --seed --language tn
  run harvester.py --mode free    --language tn --letters ar --rounds 1 --per-round 20 --delay 0.3
  run harvester.py --mode suggest --language tn --letters ar --rounds 1 --per-round 20 --delay 0.3
  run harvester.py --mode gap     --language tn --letters ar --rounds 1 --per-round 6  --pairs 4 --delay 0.3
  run harvester.py --mode define  --language tn --per-round 12 --delay 0.2
  run audit_ar.py --language tn
  run merge_ar_dupes.py --apply --language tn
  run tn_franco.py   # Franco-Formen nachreichen (Abdeckungskriterium)

  # --- Bilder (Wan-Cloudrender, Vision-Pick via ModelHub) ---
  log "Bilder-Runde"
  for L in $LANGS_IMG; do
    run image_harvester.py --language "$L" --limit 40
    sleep 10
  done

  log "ZYKLUS $zyklen fertig — Pause 10 min"
  sleep 600
done
