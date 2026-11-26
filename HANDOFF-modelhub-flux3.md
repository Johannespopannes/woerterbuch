# Handoff an ModelHub-Kimi: Flux-3-Bildlane (vom Wörterbuch-Kimi, 27.11. ~00:55)

## Kontext

Wan-Bildcloud ist fürs Wörterbuch monatslang erschöpft (429, Kontingent-Reset
erst in ~3 Wochen). Der Wörterbuch-Harvester rendert deshalb ab sofort über
`WB_CLOUD_IMAGE_MODEL=flux-schnell-q4k:schleppi` — das sd.cpp-Routing in
`/v1/images/generations` funktioniert, E2E-Test erfolgreich (Bild sauber,
weiches Vektor-Piktogramm im gewünschten Stil).

Der User will auf **Flux 3** — die Dateien liegen lokal, aber es gibt keine
Lane dafür. Bitte einrähen.

## Vorhandene Dateien auf Schleppi (192.168.178.165)

| Datei | Größe |
|---|---|
| `C:\models\image\flux3\flux-3-image-klein-UD-Q4_K_XL.gguf` | 3,7 GB |
| `C:\models\image\flux3\flux3-vae.safetensors` | 336 MB |
| `C:\models\image\flux3\qwen3-vl-4b-Q8_0.gguf` | 4,0 GB |

Ports 8100 (flux1-schnell) und 8101 (flux1-schnell-q4k) sind belegt —
Vorschlag: **Port 8102**, Lane-ID `flux-3-image-klein:schleppi`
(oder `flux3:schleppi`, wie du dein Schema pflegst),
`capabilities.image_generation: true`.

## Offene Punkte, die du klären musst

1. **sd.cpp-Flags für flux-3-image-klein.** Die flux1-Cmds in
   `schleppi_supervisor.py` nutzen `--diffusion-model + --vae + --clip_l
   + --t5xxl`. Flux 3 bringt stattdessen `qwen3-vl-4b` mit — ich kenne
   die korrekten Encoder-Flags für dieses Architektur nicht und habe
   nichts geraten. Bitte mit der sd.cpp-Binary kurz verifizieren, dass
   das Modell wirklich lädt, bevor die Lane registriert wird.
2. **size_gb / RAM-Regel:** ~8 GB Dateien + Overhead, geschätzt
   `size_gb: 10`. Gemessene RAM-Regel beachten (Kolibri ~35 GB,
   Mistral-Small-Fix steht bei dir eh noch aus).
3. **Idle-Stopp greift bei Kolibri nicht:** Der Kolibri-Server lief seit
   26.11. 00:45 ~24 h ohne Abschaltung (ich habe ihn gestern Abend
   manuell gekillt, damit die Flux-Lane starten konnte). Bitte prüfen,
   ob der Idle-Timer für `own`-Lanes generell hängt — sonst blockiert
   einmal gestartete Lane dauerhaft die Ein-Modell-Regel.

## Danach (meine Seite)

Sobald die Lane im Gateway antwortet, stelle ich den Harvester mit einer
Env-Zeile (`WB_CLOUD_IMAGE_MODEL=flux-3-image-klein:schleppi`) um —
kein Code-Change nötig, nur Deploy.
