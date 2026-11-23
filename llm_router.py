# -*- coding: utf-8 -*-
"""LLM-Modellrouter: verwaltet die lokalen llama-server als Katalog.

Haerte-Regeln (nach den Abstuerzen 22.11.):
  * maximal EIN grosses LLM gleichzeitig resident (Load-on-Demand,
    Idle-Unload)
  * vor jedem Load RAM-Waechter: freier RAM muss Modellgroesse*1,15 +
    Reserve abdecken, sonst wird der Load verweigert (507) — nie wieder
    Windows an die Kante treiben
  * alle Inferenz-Anfragen laufen ueber eine globale Warteschlange
    (Serialisierung, ein aktiver Job)

Endpoints:
  GET  /models               Katalog mit Status (loaded/running/offline)
  GET  /status               aktives Modell, RAM, Queue
  POST /load    {"id"}       Modell starten (managed Port aus Config)
  POST /unload  {"id"}       Modell stoppen
  POST /v1/chat/completions  OpenAI-kompatibler Proxy (queued, laedt
                             Standard-Modell bei Bedarf)
  GET  /v1/models            OpenAI-kompatibel: aktives Modell

Start (via daemon-start.py oder manuell):
  python llm_router.py            # Port 8090
Konfiguration: llm_models.json (neben diesem Skript).
"""
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "llm_models.json"

app = FastAPI(title="Wörterbuch-LLM-Router")

# ---------- Konfiguration ----------
CONFIG: dict = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MODELS: dict[str, dict] = {m["id"]: m for m in CONFIG["models"]}

# ---------- Zustand ----------
state: dict = {
    "pids": {},        # model_id -> Popen-Prozess
    "last_used": 0.0,  # letzte Inferenz (fuer Idle-Unload)
    "busy": False,
}
inferenz_lock = asyncio.Lock()


def log(msg: str) -> None:
    print(f"[router {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- RAM-Waechter ----------
class _MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def freier_ram_bytes() -> int:
    st = _MEMSTAT()
    st.dwLength = ctypes.sizeof(_MEMSTAT)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return 0
    return int(st.ullAvailPhys)


def modell_groesse_bytes(model: dict) -> int:
    total = 0
    for p in model.get("gguf") or []:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def lokal_vorhanden(model: dict) -> bool:
    gguf = model.get("gguf") or []
    return bool(gguf) and all(os.path.exists(p) for p in gguf)


# ---------- Prozessverwaltung ----------
def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def server_laeuft(model: dict) -> bool:
    return http_ok(f"http://127.0.0.1:{model['port']}/v1/models")


def _start_prozess(model: dict) -> subprocess.Popen:
    exe = CONFIG["llama_server"]
    argv = [exe, "-m", model["gguf"][0], "-ngl", "99",
            "--host", "127.0.0.1", "--port", str(model["port"]),
            "--alias", model.get("alias", model["id"]),
            "-c", str(model.get("ctx", 8192))]
    if len(model.get("gguf") or []) > 1:
        argv += ["-m", *model["gguf"][1:]]
    if model.get("mmproj"):
        argv += ["--mmproj", model["mmproj"]]
    argv += list(model.get("extra_args") or [])
    log_dir = Path(CONFIG.get("log_dir", BASE))
    log_dir.mkdir(parents=True, exist_ok=True)
    f = open(log_dir / f"router-{model['id']}.log", "ab", buffering=0)
    DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=f,
                            stderr=subprocess.STDOUT, creationflags=DETACHED,
                            close_fds=True)


def _stop_prozess(proc: subprocess.Popen, model: dict) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=30)
    except Exception as e:
        log(f"taskkill {model['id']}: {e}")
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def aktives_modell() -> str | None:
    for mid, proc in state["pids"].items():
        if proc.poll() is None and server_laeuft(MODELS[mid]):
            return mid
    for mid in state.get("extern", set()):
        if server_laeuft(MODELS[mid]):
            return mid
    return None


# ---------- API ----------
@app.get("/models")
def models_liste() -> dict:
    out = []
    for m in CONFIG["models"]:
        eintrag = {
            "id": m["id"], "port": m.get("port"),
            "loaded": m["id"] in state["pids"] and
                      state["pids"][m["id"]].poll() is None,
            "lokal": lokal_vorhanden(m),
            "offline": bool(m.get("offline")),
            "keep_loaded": bool(m.get("keep_loaded")),
            "sprachen": m.get("sprachen", []),
            "size_gb": round(modell_groesse_bytes(m) / 1e9, 1),
        }
        if m.get("notiz"):
            eintrag["notiz"] = m["notiz"]
        out.append(eintrag)
    return {"models": out,
            "freier_ram_gb": round(freier_ram_bytes() / 1e9, 1)}


@app.get("/status")
def status() -> dict:
    return {
        "aktiv": aktives_modell(),
        "freier_ram_gb": round(freier_ram_bytes() / 1e9, 1),
        "busy": state["busy"],
        "idle_minuten": CONFIG.get("idle_minutes", 20),
    }


@app.post("/load")
async def load(body: dict) -> dict:
    mid = body.get("id", "")
    if mid not in MODELS:
        raise HTTPException(404, f"Modell '{mid}' nicht im Katalog")
    model = MODELS[mid]
    if not lokal_vorhanden(model):
        raise HTTPException(409, f"Modell '{mid}' nicht lokal vorhanden "
                                 f"(offline-Eintrag, per llm_models.py "
                                 f"--fetch {mid} laden)")
    if mid in state["pids"] and state["pids"][mid].poll() is None:
        return {"ok": True, "id": mid, "bereits": True}
    if server_laeuft(model):
        # Extern gestartet (z. B. von daemon-start.py) — übernehmen
        state.setdefault("extern", set()).add(mid)
        state["last_used"] = time.time()
        return {"ok": True, "id": mid, "bereits": True, "extern": True}
    # RAM-Waechter zuerst: lieber verweigern, als ein laufendes Modell
    # ggf. umsonst zu entladen
    reserve = int(CONFIG.get("ram_reserve_gb", 8) * 1e9)
    bedarf = int(modell_groesse_bytes(model) * 1.15) + reserve
    frei = freier_ram_bytes()
    if frei < bedarf:
        raise HTTPException(
            507, f"Nicht genug freier RAM: {frei/1e9:.1f} GB frei, "
                 f"{bedarf/1e9:.1f} GB noetig (Modell + KV + Reserve). "
                 f"Load verweigert, um Windows-Stabilitaet zu schuetzen.")
    # Genau ein residentes LLM: anderes Modell erst entladen
    for anderes, proc in list(state["pids"].items()):
        if anderes == mid:
            continue
        if proc.poll() is None:
            if MODELS[anders].get("keep_loaded") and not body.get("force"):
                raise HTTPException(409,
                                    f"'{anders}' ist Dauer-Modell (keep_loaded) "
                                    f"— force=true noetig, um zu rotieren")
            log(f"entlade '{anders}' (Rotation zu '{mid}')")
            _stop_prozess(proc, MODELS[anders])
            del state["pids"][anders]
    proc = _start_prozess(model)
    state["pids"][mid] = proc
    deadline = time.time() + 600
    while time.time() < deadline:
        if http_ok(f"http://127.0.0.1:{model['port']}/v1/models",
                   timeout=5.0):
            log(f"'{mid}' bereit auf Port {model['port']}")
            return {"ok": True, "id": mid, "port": model["port"]}
        if proc.poll() is not None:
            del state["pids"][mid]
            raise HTTPException(500, f"llama-server fuer '{mid}' ist "
                                     f"abgestuerzt (Log: router-{mid}.log)")
        await asyncio.sleep(5)
    raise HTTPException(504, f"'{mid}' startet nach 10 min noch "
                             f"(Modell gross?)")


@app.post("/unload")
async def unload(body: dict) -> dict:
    mid = body.get("id", "")
    proc = state["pids"].get(mid)
    if not proc:
        raise HTTPException(404, f"'{mid}' laeuft nicht unter Router-Kontrolle")
    if MODELS[mid].get("keep_loaded") and not body.get("force"):
        raise HTTPException(409, f"'{mid}' ist Dauer-Modell; force=true noetig")
    _stop_prozess(proc, MODELS[mid])
    del state["pids"][mid]
    log(f"'{mid}' entladen")
    return {"ok": True, "id": mid}


@app.get("/v1/models")
def openai_models() -> dict:
    aktiv = aktives_modell()
    data = []
    if aktiv:
        data.append({"id": MODELS[aktiv].get("alias", aktiv),
                     "object": "model", "created": 0, "owned_by": "router"})
    return {"object": "list", "data": data}


async def _proxy_inferenz(model: dict, body: bytes, headers: dict) -> Response:
    url = f"http://127.0.0.1:{model['port']}/v1/chat/completions"
    hdrs = {"Content-Type": "application/json"}
    auth = headers.get("authorization")
    if auth:
        hdrs["Authorization"] = auth
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=1800) as r:
        return Response(content=r.read(), status_code=r.status,
                        media_type="application/json")


@app.post("/v1/chat/completions")
async def chat(req: Request) -> Response:
    body = await req.body()
    async with inferenz_lock:          # Serialisierung: 1 aktiver Job
        state["busy"] = True
        try:
            aktiv = aktives_modell()
            if not aktiv:
                kandidaten = [m for m in CONFIG.get("autostart", [])
                              if m in MODELS and lokal_vorhanden(MODELS[m])]
                ziel = kandidaten[0] if kandidaten else None
                if not ziel:
                    raise HTTPException(503, "Kein Modell geladen und kein "
                                             "autostart-Modell verfuegbar")
                log(f"Load-on-Demand: starte '{ziel}'")
                await load({"id": ziel})
                aktiv = ziel
            state["last_used"] = time.time()
            try:
                return await _proxy_inferenz(MODELS[aktiv], body,
                                             dict(req.headers))
            finally:
                state["last_used"] = time.time()
        finally:
            state["busy"] = False


# ---------- Idle-Unload ----------
async def idle_watcher() -> None:
    """Entlaedt Nicht-Dauer-Modelle nach Inaktivitaet (Load-on-Demand)."""
    grenze = CONFIG.get("idle_minutes", 20) * 60
    while True:
        await asyncio.sleep(60)
        if state["busy"] or not state["last_used"]:
            continue
        if time.time() - state["last_used"] < grenze:
            continue
        for mid, proc in list(state["pids"].items()):
            if proc.poll() is None and not MODELS[mid].get("keep_loaded"):
                log(f"Idle-Unload: '{mid}' nach "
                    f"{grenze/60:.0f} min Inaktivitaet entladen")
                _stop_prozess(proc, MODELS[mid])
                del state["pids"][mid]
        state["last_used"] = time.time() if state["pids"] else 0.0


@app.on_event("startup")
async def _startup() -> None:
    # Bereits laufende Server unter Router-Kontrolle nehmen (z. B. Jais,
    # den daemon-start.py frueher direkt gestartet hat)
    for mid, model in MODELS.items():
        if server_laeuft(model):
            log(f"'{mid}' laeuft bereits auf Port {model['port']} "
                f"(extern gestartet, uebernommen)")
            state.setdefault("extern", set()).add(mid)
            state["last_used"] = time.time()
    asyncio.create_task(idle_watcher())
    log(f"Router bereit, Katalog: {len(MODELS)} Modelle, "
        f"freier RAM {freier_ram_bytes()/1e9:.1f} GB")


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get(
        "ROUTER_PORT", 8090)), log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
