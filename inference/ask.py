#!/usr/bin/env python3
"""Domanda singola: retrieval chunk+grafo → risposta LM Studio."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rag.config import load_config
from rag.session import open_session


def main() -> None:
    ap = argparse.ArgumentParser(description="GraphRAG lite — una domanda")
    ap.add_argument("question", help="domanda in italiano")
    ap.add_argument("--config", default=None, help="path config.yaml")
    ap.add_argument(
        "--verbose", "-v", action="store_true", help="mostra log retrieval"
    )
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="solo retrieval, senza chiamata LM Studio",
    )
    ap.add_argument(
        "--stream",
        action="store_true",
        help="risposta in streaming",
    )
    ap.add_argument(
        "--save-log",
        default=None,
        help="salva JSON con retrieval + risposta",
    )
    args = ap.parse_args()

    session = open_session(
        args.config,
        quiet=not args.verbose,
        connect_llm=not args.no_llm,
    )
    result, context = session.retrieve(args.question)

    if args.verbose:
        print("--- RETRIEVAL ---")
        print(f"Chunk: {[c.chunk_id for c in result.chunks]}")
        print(f"Seed nodi: {len(result.seed_node_ids)}")
        print(f"Nodi totali (1 hop): {len(result.graph_node_ids)}")
        print(f"Archi: {len(result.graph_edges)}")
        print("--- CONTESTO (anteprima) ---")
        preview = context[:2000] + ("…" if len(context) > 2000 else "")
        print(preview)
        print("---")

    answer = ""
    if not args.no_llm:
        if args.stream:
            for token in session.stream_answer(args.question, context=context):
                print(token, end="", flush=True)
            print()
            if session.history:
                answer = session.history[-1]["content"]
        else:
            answer = session.answer(args.question, context=context)
            print(answer)
    elif args.verbose:
        print("(nessuna chiamata LLM, --no-llm attivo)")

    if args.save_log:
        log_path = Path(args.save_log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "question": args.question,
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "score": c.score,
                    "part": c.part_title,
                }
                for c in result.chunks
            ],
            "seed_node_ids": sorted(result.seed_node_ids),
            "graph_node_ids": sorted(result.graph_node_ids),
            "graph_edge_count": len(result.graph_edges),
            "model": session.llm.model_id if session.llm else "",
            "answer": answer,
        }
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if args.verbose:
            print(f"Log salvato: {log_path}")


if __name__ == "__main__":
    main()
