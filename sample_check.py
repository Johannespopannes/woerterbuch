"""Stichproben-Report: Qualität der arabischen Verifikation beurteilen.

Zieht zufällig N verifizierte und N unsicher markierte Einträge einer
Sprache und schreibt einen Markdown-Report (Wort, Verifikationsquelle,
Begründung, Definition) zur manuellen Beurteilung.

Aufruf: python sample_check.py --language ar --n 20 [--seed 42]
Env: DICT_API_URL, DICT_API_KEY
"""

import argparse
import json
import os
import random
import urllib.error
import urllib.request

API = os.environ["DICT_API_URL"].rstrip("/")
KEY = os.environ["DICT_API_KEY"]


def api_get(path: str) -> list:
    req = urllib.request.Request(f"{API}{path}",
                                 headers={"X-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_all(language: str) -> list[dict]:
    out, offset = [], 0
    while True:
        batch = api_get(f"/entries?language={language}"
                        f"&limit=1000&offset={offset}&with_evidence=true")
        out.extend(batch)
        if len(batch) < 1000:
            return out
        offset += 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--language", default="ar")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="sample_check_ar.md")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    entries = fetch_all(args.language)
    verified = [e for e in entries if e.get("evidence")
                and not e.get("sprache_unsicher")]
    unsicher = [e for e in entries if e.get("evidence")
                and e.get("sprache_unsicher")]
    v_pick = random.sample(verified, min(args.n, len(verified)))
    u_pick = random.sample(unsicher, min(args.n, len(unsicher)))

    lines = [f"# Stichprobe {args.language}: {args.n} verifiziert + "
             f"{args.n} unsicher", "",
             f"Bestand mit Evidenz: {len(entries)} "
             f"(verifiziert {len(verified)}, unsicher {len(unsicher)})", "",
             "## Verifiziert (Quelle + Definition)"]
    for e in sorted(v_pick, key=lambda x: x["word"]):
        ev = e["evidence"] or {}
        lines.append(f"- **{e['word']}** — {ev.get('quelle', '?')} — "
                     f"{(e.get('definition') or '—')[:120]}")
    lines += ["", "## Als unsicher markiert (Begründung + Definition)"]
    for e in sorted(u_pick, key=lambda x: x["word"]):
        ev = e["evidence"] or {}
        lines.append(f"- **{e['word']}** — {ev.get('hinweis', '—')[:160]} — "
                     f"{(e.get('definition') or '—')[:120]}")
    report = "\n".join(lines) + "\n"
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report: {args.out}")
    print(report[:3000])


if __name__ == "__main__":
    main()
