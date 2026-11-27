# Handoff an ModelHub-Kimi: Korrektur + ein offener Bug (27.11. ~20:30)

## Korrektur: „Flux 3" war ein Missverständnis

Der User meinte mit „auf Flux 3" **Flux für die 3 Wochen**, die das
Wan-Kontingent noch weg ist — nicht das Flux-3-Modell. Eine neue Bildlane
braucht es also NICHT; der Wörterbuch-Harvester rendert jetzt über die
vorhandene `flux-schnell-q4k:schleppi`-Lane, das sd.cpp-Routing im Gateway
funktioniert E2E. Die Dateien unter `C:\models\image\flux3\` bleiben erst
mal dein Terrain, falls du sie später als Lane willst.

## Der eine echte Befund: Kolibri-Idle-Stopp hängt

Der Kolibri-llama-server lief auf Schleppi seit 26.11. 00:45 ~24 h ohne
Abschaltung und hat über die Supervisor-Ein-Modell-Regel die Flux-Bildlane
blockiert (409 modelhub_busy). Ich habe ihn am 26.11. abend manuell gekillt;
der Flux-Render ging danach sofort durch. Bitte prüfen, ob der Idle-Timer
für `own`-Lanes generell hängt — sonst blockiert eine einmal gestartete Lane
den Bildpfad dauerhaft, und der neue 409-Backoff im Harvester würde dann
zwar nichts kaputt machen, aber bis zu 8× umsonst warten.
