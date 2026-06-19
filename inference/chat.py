#!/usr/bin/env python3
"""Chat interattiva con streaming — sessione persistente, retrieval per turno."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rag.env import api_provider_label, list_api_provider_names, load_inference_env
from rag.session import open_session

HELP = """
Comandi:
  /help      — questo messaggio
  /quit      — esci (anche /exit, Ctrl+C)
  /clear     — azzera la cronologia conversazione
  /verbose   — toggle log retrieval a ogni turno
  /retrieval — mostra chunk/nodi dell'ultimo turno
"""


def _print_retrieval(session) -> None:
    r = session.last_result
    if not r:
        print("(nessun retrieval ancora)")
        return
    print(f"Chunk: {[c.chunk_id for c in r.chunks]}")
    print(f"Nodi grafo: {len(r.graph_node_ids)} | Archi: {len(r.graph_edges)}")


def _backend_label(session, use_api: str | None) -> str:
    if use_api and session.llm:
        pid = session.llm_provider or use_api
        return f"{api_provider_label(pid)} ({session.llm.model_id})"
    if session.llm:
        return f"LM Studio ({session.llm.model_id})"
    return "LLM"


def main() -> None:
    load_inference_env()

    ap = argparse.ArgumentParser(description="Chat Adriano — GraphRAG + streaming")
    ap.add_argument("--config", default=None, help="path config.yaml")
    ap.add_argument(
        "--verbose", "-v", action="store_true", help="log retrieval ad ogni turno"
    )
    ap.add_argument(
        "--no-stream",
        action="store_true",
        help="risposta intera senza streaming (debug)",
    )
    ap.add_argument(
        "--timing",
        action="store_true",
        help="stampa breakdown tempi retrieval prima dello streaming",
    )
    api_examples = ", ".join(list_api_provider_names()) or "groq, deepseek"
    ap.add_argument(
        "--use_API",
        dest="use_api",
        metavar="NAME",
        default=None,
        help=f"provider API remoto (es. {api_examples} — deve coincidere con *_NAME_ID in .env)",
    )
    args = ap.parse_args()

    session = open_session(args.config, use_api=args.use_api)
    verbose = args.verbose
    backend = _backend_label(session, args.use_api)

    print(f"Adriano — Memorie [{backend}]. Scrivi una domanda; /help per comandi.\n")

    try:
        while True:
            try:
                line = input("Tu: ").strip()
            except EOFError:
                print()
                break
            if not line:
                continue

            cmd = line.lower()
            if cmd in ("/quit", "/exit", "quit", "exit"):
                break
            if cmd == "/help":
                print(HELP.strip())
                continue
            if cmd == "/clear":
                session.clear_history()
                print("Cronologia azzerata.")
                continue
            if cmd == "/verbose":
                verbose = not verbose
                print(f"Verbose retrieval: {'on' if verbose else 'off'}")
                continue
            if cmd == "/retrieval":
                _print_retrieval(session)
                continue

            _, context = session.retrieve(line)
            if verbose:
                print("--- retrieval ---", file=sys.stderr)
                _print_retrieval(session)
            if args.timing or verbose:
                session.log_timings()

            print("\nAdriano: ", end="", flush=True)
            if args.no_stream:
                answer = session.answer(line, context=context)
                print(answer)
            else:
                for token in session.stream_answer(line, context=context):
                    print(token, end="", flush=True)
                print()
            print()

    except KeyboardInterrupt:
        print("\nUscita.")

    print("Arrivederci.")


if __name__ == "__main__":
    main()
