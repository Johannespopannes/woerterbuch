# -*- coding: utf-8 -*-
"""Vorläufige Bedeutungen regulär sprachlich prüfen und korrigieren."""
import argparse
import json
import os
import re
import time

from harvester import api_get, api_patch, post_chat_completions


PROMPT = (
    "Prüfe vorläufige Wörterbuchbedeutungen sprachwissenschaftlich. "
    "Erfundene, bloß aus der Schreibweise abgeleitete oder nicht belastbar "
    "belegte Bedeutungen sind valid=false. Korrigiere bei valid=true Glosse, "
    "Wortart und eine präzise Beschreibung in der Sprache des Worts. "
    "Antworte NUR als JSON-Objekt mit den Nummern als Schlüsseln: "
    '{"1":{"valid":true,"label":"...","wortart":"...",'
    '"beschreibung":"..."},"2":{"valid":false}}.\n\n'
)


def pruefen(model: str, rows: list[dict]) -> dict:
    zeilen = "\n".join(
        f"{i + 1}. {r['word']} — {r['label']}: {r.get('text') or ''}"
        for i, r in enumerate(rows))
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT + zeilen}],
        "max_tokens": 1200,
        "temperature": 0,
    }
    try:
        result = post_chat_completions(
            body, int(os.environ.get("MODEL_TIMEOUT", "240")))
        text = result["choices"][0]["message"].get("content") or ""
        match = re.search(r"\{.*\}", text, re.S)
        raw = json.loads(match.group(0)) if match else {}
    except Exception:
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--language", required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--model", default=os.environ.get(
        "MODEL_NAME", "mistral-large-4:cloud"))
    args = ap.parse_args()
    rows = api_get(
        f"/senses/review-queue?language={args.language}&limit={args.limit}"
    ).get("rows", [])
    if not rows:
        print(f"{args.language}: keine vorläufigen Bedeutungen zu prüfen")
        return
    stats = {"reviewed": 0, "rejected": 0, "offen": 0}
    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        results = pruefen(args.model, chunk)
        for index, row in enumerate(chunk, 1):
            result = results.get(str(index))
            if not result or not isinstance(result.get("valid"), bool):
                stats["offen"] += 1
                continue
            valid = result["valid"]
            api_patch(f"/senses/{row['sense_id']}/review", {
                "valid": valid,
                "label": str(result.get("label") or row["label"])[:200],
                "word_class": str(
                    result.get("wortart") or row.get("word_class") or "")[:40],
                "description": str(
                    result.get("beschreibung") or row.get("text") or "")[:1000],
                "source": f"sense-review:{args.model}"[:40],
            })
            stats["reviewed" if valid else "rejected"] += 1
        print(f"{min(start + args.batch, len(rows))}/{len(rows)}: {stats}",
              flush=True)
        time.sleep(args.delay)
    print(f"{args.language}: Bedeutungsprüfung fertig: {stats}")


if __name__ == "__main__":
    main()
