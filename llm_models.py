# -*- coding: utf-8 -*-
"""Download-Helfer fuer Offline-Modelle im llm_models.json-Katalog.

Nur Infrastruktur: laedt fehlende GGUFs von Hugging Face herunter,
der Router erkennt sie danach automatisch als 'lokal vorhanden'.

Aufrufe:
  python llm_models.py --list            # Katalog mit Status anzeigen
  python llm_models.py --fetch llm-jp-4.1-33b
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "llm_models.json"


def katalog() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def eintrag(konf: dict, mid: str) -> dict:
    for m in konf["models"]:
        if m["id"] == mid:
            return m
    sys.exit(f"Modell '{mid}' nicht im Katalog ({CONFIG_PATH})")


def vorhanden(m: dict) -> bool:
    gguf = m.get("gguf") or []
    return bool(gguf) and all(Path(p).exists() for p in gguf)


def cmd_list(konf: dict) -> None:
    frei = None
    try:
        import llm_router
        frei = llm_router.freier_ram_bytes() / 1e9
    except Exception:
        pass
    print(f"{'id':22} {'RAM-GB':>7} {'lokal':>6} {'keep':>5}  Sprachen")
    for m in konf["models"]:
        size = 0
        for p in m.get("gguf") or []:
            try:
                size += Path(p).stat().st_size / 1e9
            except OSError:
                pass
        print(f"{m['id']:22} "
              f"{size:>7.1f} "
              f"{str(vorhanden(m)):>6} "
              f"{str(bool(m.get('keep_loaded'))):>5}  "
              f"{', '.join(m.get('sprachen', []))}")
        if m.get('notiz'):
            print(f"{'':22} ↳ {m['notiz']}")
    if frei is not None:
        print(f"\nFreier RAM: {frei:.1f} GB")


def cmd_fetch(konf: dict, mid: str) -> None:
    m = eintrag(konf, mid)
    if not m.get("offline") or not m.get("hf_repo"):
        if vorhanden(m):
            sys.exit(f"'{mid}' ist lokal bereits vorhanden.")
        sys.exit(f"'{mid}' hat kein hf_repo — Download nicht moeglich.")
    print(f"Lade '{mid}' von {m['hf_repo']} … (ggf. Login via "
          f"'hf auth login' noetig)")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("huggingface_hub fehlt: pip install huggingface_hub")
    try:
        import hf_transfer  # noqa: F401
        transfer = ["--enable_hf_transfer"]
    except ImportError:
        transfer = []
    ziel = Path(m["gguf"][0]).parent
    ziel.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=m["hf_repo"],
                      local_dir=str(ziel),
                      allow_patterns=["*.gguf"],
                      **({"enable_hf_transfer": True} if transfer else {}))
    print(f"Fertig: {ziel}")
    if not vorhanden(m):
        print("WARNUNG: Dateien liegen unter dem erwarteten Pfad nicht "
              "vor — gguf-Pfade in llm_models.json pruefen.", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fetch")
    a = ap.parse_args()
    konf = katalog()
    if a.fetch:
        cmd_fetch(konf, a.fetch)
    else:
        cmd_list(konf)


if __name__ == "__main__":
    main()
