# Handoff an ModelHub-Kimi: Ein-Modell-Regel vs. zwei Nutzer (27.11. ~22:50)

## Korrektur: „Flux 3" war ein Missverständnis

Der User meinte mit „auf Flux 3" **Flux für die 3 Wochen**, die das
Wan-Kontingent noch weg ist — nicht das Flux-3-Modell. Eine neue Bildlane
braucht es also NICHT; der Wörterbuch-Harvester rendert über die
vorhandene `flux-schnell-q4k:schleppi`-Lane, E2E getestet. Die Dateien
unter `C:\models\image\flux3\` bleiben erst mal dein Terrain.

## Der echte Blocker: Ein-Modell-Regel trifft jetzt BEIDE Systeme

Der Wörterbuch-Bildpfad ist jetzt in die icons-GPU-Queue eingereiht
(WB_RENDER_QUEUE=1) — rendert also über dieselbe Flux-Lane wie die
Icons-Jobs. Gleichzeitig lief bis gestern die Bild-Übersetzung über
Kolibri. Beides kollidiert an der Supervisor-Ein-Modell-Regel, und der
hängende Idle-Stopp macht es dauerhaft:

- **27.11. 00:27–22:50:** Flux-sd-server (PID 53608) lief ~22 h ohne
  erkennbaren Job (Idle-Stopp hängt — gleiches Muster wie Kolibri am
  26.11.). Jede Kolibri-Anfrage bekam 409 modelhub_busy; die
  Wörterbuch-Bilder-Übersetzung fraß 6×20 s Backoff pro Wort und
  verworfene Motive.
- RAM-rechnung: Kolibri ~35 GB + Flux ~12 GB = ~47 GB < 96 GB — beide
  könnten gleichzeitig laufen. Die harte Ein-Modell-Regel ist hier
  strenger nötig.

**Bitte:** (1) Idle-Stopp-Bug finden (betrifft offenbar jede `own`-Lane),
(2) Regel auf RAM-Basis lockern statt absolutem Ein-Modell.

## Workaround auf meiner Seite (bis dahin)

Bilder-Übersetzung/Pick laufen jetzt über qwen3.8-flash (Cloud) statt
Kolibri — der Bildpfad braucht keinen Schleppi-LLM-Slot mehr und reiht
sich nur noch als Render-Job in die Flux-Lane ein.
