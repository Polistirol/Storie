#!/usr/bin/env python3
"""Smoke test: 5 domande fisse con log in data/smoke/."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rag.session import open_session

SMOKE_QUESTIONS = [
    "Come mai sei andato dal tuo medico Ermogene stamattina?",
    "Che rapporto avevi con il tuo cavallo Boristene?",
    "Come vivi il pensiero della morte che si avvicina?",
    "Cosa provi quando senti lo sbuffare di un cervo nei boschi?",
    "Parlami di Antinoo: cosa significa per te?",
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Smoke test inference")
    ap.add_argument("--config", default=None)
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="solo retrieval, utile senza LM Studio",
    )
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent / "data" / "smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    session = open_session(args.config, quiet=True, connect_llm=not args.no_llm)

    summary = []
    for i, question in enumerate(SMOKE_QUESTIONS, 1):
        print(f"\n[{i}/{len(SMOKE_QUESTIONS)}] {question}")
        result, context = session.retrieve(question)
        answer = ""
        if not args.no_llm:
            answer = session.answer(question, context=context)
            print(answer)
        else:
            print(f"  chunk: {[c.chunk_id for c in result.chunks]}")

        log = {
            "question": question,
            "chunks": [
                {"chunk_id": c.chunk_id, "score": round(c.score, 4)}
                for c in result.chunks
            ],
            "graph_nodes": len(result.graph_node_ids),
            "graph_edges": len(result.graph_edges),
            "model": session.llm.model_id if session.llm else "",
            "answer": answer,
        }
        log_path = out_dir / f"{stamp}_q{i}.json"
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        summary.append(log)

    summary_path = out_dir / f"{stamp}_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nSmoke completato. Log in {out_dir}")


if __name__ == "__main__":
    main()
