# -*- coding: utf-8 -*-
"""Konsistenz des LLM-Modellkatalogs (llm_models.json) + Router-Import."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def test_katalog_parst():
    cfg = json.loads((BASE / "llm_models.json").read_text(encoding="utf-8"))
    assert cfg["models"], "Katalog leer"
    assert Path(cfg["llama_server"]).exists(), "llama-server.exe fehlt"


def test_katalog_ids_und_ports_eindeutig():
    cfg = json.loads((BASE / "llm_models.json").read_text(encoding="utf-8"))
    ids = [m["id"] for m in cfg["models"]]
    ports = [m.get("port") for m in cfg["models"] if m.get("port")]
    assert len(ids) == len(set(ids)), "doppelte Modell-IDs"
    assert len(ports) == len(set(ports)), "doppelte Ports"


def test_autostart_modelle_im_katalog():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_router", BASE / "llm_router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # noqa: S102 — eigener Code
    for mid in mod.CONFIG.get("autostart", []):
        assert mid in mod.MODELS, f"autostart '{mid}' fehlt im Katalog"


def test_ram_waechter_rechnet():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_router", BASE / "llm_router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # noqa: S102 — eigener Code
    bedarf = mod.modell_groesse_bytes(mod.MODELS["jais-3-ar"]) * 1.15 + 8e9
    assert bedarf < 35e9, "Jais-Bedarf über Reserve-Grenze — RAM-Regel prüfen"
