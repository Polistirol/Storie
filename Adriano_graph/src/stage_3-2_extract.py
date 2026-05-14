#!/usr/bin/env python
"""
src/stage_3-2_extract.py
Stage 3.2 — estrazione knowledge graph chunk per chunk.

Legge i chunk da `data/stage_2/chunks.json`, per ognuno chiama Claude con
il prompt e gli esempi few-shot definiti in `stage_3-1_prompt.py`, raccoglie
l'output del tool `submit_extraction`, arricchisce con `Provenance`,
valida con i modelli Pydantic di `schema.py`, scrive il risultato cumulativo
in `data/stage_3/extracted_graph.json` e un log dettagliato.

Workflow tipico:
    # 1. Smoke test sui 4 chunk di test (Pila B): default
    python src/stage_3-2_extract.py --output data/stage_3/extracted_graph_test.json --log data/stage_3/extraction_log_test.json

    # 2. Estrazione su tutti i 310 chunk dopo aver validato il prompt
    python src/stage_3-2_extract.py --all

    # 3. Ispezione del payload senza chiamare l'API
    python src/stage_3-2_extract.py --chunks ch_0001 --dry-run

Le chiamate sono sequenziali per debug e per massimizzare l'hit rate del
prompt caching (ADR-013): chiamate parallele potrebbero servire la cache
prima che il primo write sia ack'd.

NB sui nomi file: questo script importa `stage_3-1_prompt.py`, che ha un
trattino nel nome e quindi NON è importabile con `import` standard. Uso
`importlib.util.spec_from_file_location`. Se il file viene rinominato con
underscore (`stage_3_1_prompt.py`) lo si può sostituire con un normale
`from src.stage_3_1_prompt import ...` togliendo la sezione di bootstrap.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# -----------------------------------------------------------------------------
# Bootstrap: sys.path + import del modulo prompt (file con trattino)
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent       # Adriano_graph/
REPO_ROOT = PROJECT_ROOT.parent                              # Storie/
sys.path.insert(0, str(PROJECT_ROOT))

_SRC_DIR = Path(__file__).resolve().parent
# Cerca prima il nome con trattino (post-rinomina), poi fallback al nome originale.
_PROMPT_CANDIDATES = ("stage_3-1_prompt.py", "stage_3_prompt.py")
_PROMPT_FILE = next((_SRC_DIR / n for n in _PROMPT_CANDIDATES if (_SRC_DIR / n).exists()), None)
if _PROMPT_FILE is None:
    raise FileNotFoundError(
        f"Non trovo nessuno tra {_PROMPT_CANDIDATES} in {_SRC_DIR}."
    )
_spec = importlib.util.spec_from_file_location("stage_3_1_prompt", _PROMPT_FILE)
assert _spec is not None and _spec.loader is not None
prompt_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prompt_mod)

from src.schema import (  # noqa: E402  (import dopo sys.path bootstrap)
    SCHEMA_VERSION,
    Edge,
    EdgeType,
    ExtractedGraph,
    Node,
    NodeType,
    Provenance,
    is_edge_valid,
)


# -----------------------------------------------------------------------------
# Env / SDK
# -----------------------------------------------------------------------------

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)
except ImportError:
    pass  # se non c'è dotenv, ci si aspetta ANTHROPIC_API_KEY già nell'env

import anthropic  # noqa: E402
from pydantic import ValidationError  # noqa: E402


# -----------------------------------------------------------------------------
# Costanti
# -----------------------------------------------------------------------------

STAGE_VERSION = "0.1.0"
DEFAULT_MODEL = "claude-sonnet-4-6"  # ADR-010; correggere con --model se l'alias non risponde
DEFAULT_MAX_TOKENS = 8000
DEFAULT_TEMPERATURE = 0.0

CHUNKS_PATH = PROJECT_ROOT / "data" / "stage_2" / "chunks.json"
CHUNK_SELECTED_PATH = PROJECT_ROOT / "data" / "stage_3" / "chunk_selected.json"
TEST_DIR = PROJECT_ROOT / "data" / "stage_3" / "test"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "stage_3" / "extracted_graph.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "data" / "stage_3" / "extraction_log.json"


logger = logging.getLogger("stage_3_extract")


# -----------------------------------------------------------------------------
# Caricamento e selezione
# -----------------------------------------------------------------------------

def load_chunks() -> dict[str, dict]:
    """Indice chunk_id -> chunk completo dal `chunks.json`."""
    with CHUNKS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {c["chunk_id"]: c for c in data["chunks"]}


def resolve_chunk_selection(args: argparse.Namespace) -> list[str]:
    """Ritorna la lista ordinata di chunk_id da processare in base agli argomenti."""
    if args.chunks:
        return list(args.chunks)

    if args.all:
        # ordine naturale (zero-padded) garantisce idempotenza nei retry
        chunks = load_chunks()
        return sorted(chunks.keys())

    if args.from_selected:
        with CHUNK_SELECTED_PATH.open("r", encoding="utf-8") as f:
            sel = json.load(f)
        return [c for c in sel.get("test_id", []) if c]

    # Default: chunks annotati a mano in data/stage_3/test/ (Pila B)
    return sorted(p.stem for p in TEST_DIR.glob("ch_*.json"))


# -----------------------------------------------------------------------------
# Wrapping del modello: flat output -> ExtractedGraph Pydantic
# -----------------------------------------------------------------------------

def build_provenance(
    chunk_id: str,
    model: str,
    timestamp: datetime,
    confidence: float,
    evidence_span: str,
) -> Provenance:
    return Provenance(
        chunk_id=chunk_id,
        model=model,
        timestamp=timestamp,
        schema_version=SCHEMA_VERSION,
        confidence=confidence,
        evidence_span=evidence_span,
        human_validated=False,
    )


def build_extracted_graph(
    flat: dict,
    model: str,
    timestamp: datetime,
) -> tuple[ExtractedGraph, list[dict]]:
    """
    Converte la shape flat ricevuta dal modello in `ExtractedGraph` Pydantic,
    ricostruendo la `Provenance` per ogni nodo/arco (ADR-012).

    Filtra gli archi che violano `EDGE_COMPATIBILITY` o che puntano a nodi
    non estratti, restituendoli come lista a parte per il log.
    """
    chunk_id = flat["chunk_id"]

    nodes_pyd: list[Node] = []
    for n in flat.get("nodes", []):
        nodes_pyd.append(Node(
            id=n["id"],
            type=NodeType(n["type"]),
            name=n["name"],
            description=n.get("description"),
            aliases=n.get("aliases", []),
            provenance=build_provenance(
                chunk_id=chunk_id,
                model=model,
                timestamp=timestamp,
                confidence=n["confidence"],
                evidence_span=n["evidence_span"],
            ),
        ))

    type_by_id = {n.id: n.type for n in nodes_pyd}

    edges_pyd: list[Edge] = []
    invalid_edges: list[dict] = []
    for e in flat.get("edges", []):
        try:
            edge_type = EdgeType(e["type"])
        except ValueError:
            invalid_edges.append({**e, "_reason": f"tipo arco sconosciuto: {e['type']}"})
            continue

        src_type = type_by_id.get(e["source_id"])
        tgt_type = type_by_id.get(e["target_id"])
        if src_type is None or tgt_type is None:
            invalid_edges.append({
                **e,
                "_reason": (
                    f"source_id o target_id non tra i nodi estratti: "
                    f"src={e['source_id']!r} tgt={e['target_id']!r}"
                ),
            })
            continue

        if not is_edge_valid(edge_type, src_type, tgt_type):
            invalid_edges.append({
                **e,
                "_reason": (
                    f"violazione EDGE_COMPATIBILITY: "
                    f"{src_type.value} -[{edge_type.value}]-> {tgt_type.value}"
                ),
            })
            continue

        edges_pyd.append(Edge(
            source_id=e["source_id"],
            target_id=e["target_id"],
            type=edge_type,
            description=e.get("description"),
            provenance=build_provenance(
                chunk_id=chunk_id,
                model=model,
                timestamp=timestamp,
                confidence=e["confidence"],
                evidence_span=e["evidence_span"],
            ),
        ))

    graph = ExtractedGraph(chunk_id=chunk_id, nodes=nodes_pyd, edges=edges_pyd)
    return graph, invalid_edges


# -----------------------------------------------------------------------------
# Chiamata al modello
# -----------------------------------------------------------------------------

def call_model(
    client: anthropic.Anthropic,
    payload: dict,
    max_tokens: int,
    temperature: float,
) -> tuple[Any, float]:
    """Single-shot, con timing. Lascia propagare le eccezioni al chiamante."""
    t0 = time.time()
    response = client.messages.create(
        max_tokens=max_tokens,
        temperature=temperature,
        **payload,
    )
    return response, time.time() - t0


def extract_tool_use(response: Any) -> dict:
    """Estrae l'`input` del primo blocco `tool_use` chiamato `submit_extraction`."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_extraction":
            return block.input
    raise RuntimeError(
        "Il modello non ha invocato submit_extraction nella response. "
        "Controllare stop_reason e response.content."
    )


# -----------------------------------------------------------------------------
# I/O atomico
# -----------------------------------------------------------------------------

def write_atomic_json(path: str | Path, data: Any) -> None:
    """Scrive in tmp e rinomina: l'output non resta mai in stato corrotto."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


# -----------------------------------------------------------------------------
# Aggregazione log
# -----------------------------------------------------------------------------

def build_output_envelope(
    extractions: list[dict],
    args: argparse.Namespace,
    created_at: datetime,
) -> dict:
    return {
        "source": "data/stage_2/chunks.json",
        "created_at": created_at.isoformat(),
        "stage_version": STAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": prompt_mod.PROMPT_VERSION,
        "model": args.model,
        "params": {
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "remove_description": prompt_mod.REMOVE_DESCRIPTION,
        },
        "total_chunks_processed": len(extractions),
        "extractions": extractions,
    }


def aggregate_totals(per_chunk_log: list[dict]) -> dict:
    ok = [e for e in per_chunk_log if e.get("status") == "ok"]
    return {
        "n_chunks_attempted": len(per_chunk_log),
        "n_success": len(ok),
        "n_errors": len(per_chunk_log) - len(ok),
        "total_nodes": sum(e.get("n_nodes", 0) for e in ok),
        "total_edges": sum(e.get("n_edges", 0) for e in ok),
        "total_invalid_edges": sum(e.get("n_invalid_edges", 0) for e in ok),
        "total_tokens_input": sum(e.get("tokens_input", 0) for e in ok),
        "total_tokens_output": sum(e.get("tokens_output", 0) for e in ok),
        "total_tokens_cache_creation": sum(e.get("tokens_cache_creation", 0) for e in ok),
        "total_tokens_cache_read": sum(e.get("tokens_cache_read", 0) for e in ok),
        "total_elapsed_s": round(sum(e.get("elapsed_s", 0) for e in ok), 2),
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3.2 — extraction")

    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--chunks", nargs="+", metavar="CHUNK_ID",
                     help="Lista esplicita di chunk_id (es. ch_0001 ch_0047)")
    sel.add_argument("--all", action="store_true",
                     help="Processa tutti i chunk presenti in chunks.json")
    #sel.add_argument("--from-selected", action="store_true",
    #                 help="Legge test_id da data/stage_3/chunk_selected.json")
    # Default (nessun flag): test set in data/stage_3/test/, Pila B

    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Modello Anthropic (default: {DEFAULT_MODEL}, da ADR-010)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH),
                        help="File di output cumulativo")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH),
                        help="File di log dettagliato")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stampa il payload del primo chunk e termina, senza chiamare l'API")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Se l'output esiste già, salta i chunk già estratti")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    chunks = load_chunks()
    selected = resolve_chunk_selection(args)

    if not selected:
        logger.error("Nessun chunk selezionato. Vedi --help.")
        return 1

    missing = [c for c in selected if c not in chunks]
    if missing:
        logger.error(f"chunk_id non presenti in chunks.json: {missing}")
        return 1

    logger.info(
        f"Configurazione: model={args.model} max_tokens={args.max_tokens} "
        f"temperature={args.temperature}"
    )
    logger.info(
        f"Versions: STAGE={STAGE_VERSION} SCHEMA={SCHEMA_VERSION} "
        f"PROMPT={prompt_mod.PROMPT_VERSION} REMOVE_DESCRIPTION={prompt_mod.REMOVE_DESCRIPTION}"
    )
    logger.info(f"Selezionati {len(selected)} chunk: {selected if len(selected) <= 10 else f'{selected[:5]}...'}")

    # Resume parziale
    existing_extractions: list[dict] = []
    if args.skip_existing and Path(args.output).exists():
        with open(args.output, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_extractions = existing.get("extractions", [])
        already_done = {e["chunk_id"] for e in existing_extractions}
        before = len(selected)
        selected = [c for c in selected if c not in already_done]
        logger.info(f"skip_existing: salto {before - len(selected)} chunk già estratti")

    if args.dry_run:
        first_id = selected[0]
        sample = prompt_mod.build_request_payload(
            chunk_id=first_id,
            chunk_text=chunks[first_id]["text"],
            model=args.model,
        )
        # Stampa intera struttura ma tronca i singoli content lunghi per leggibilità
        snippet = json.dumps(sample, indent=2, ensure_ascii=False)
        print(snippet[:4000])
        if len(snippet) > 4000:
            print(f"\n[...troncato. Caratteri totali: {len(snippet)}. Messages: {len(sample['messages'])}]")
        return 0

    if not selected:
        logger.info("Nulla da estrarre. Esco.")
        return 0

    client = anthropic.Anthropic()  # legge ANTHROPIC_API_KEY dall'env

    started_at = datetime.now(timezone.utc)
    extractions: list[dict] = list(existing_extractions)
    per_chunk_log: list[dict] = []

    for i, chunk_id in enumerate(selected, start=1):
        chunk_text = chunks[chunk_id]["text"]
        payload = prompt_mod.build_request_payload(
            chunk_id=chunk_id,
            chunk_text=chunk_text,
            model=args.model,
        )

        logger.info(
            f"[{i}/{len(selected)}] {chunk_id} "
            f"(token_count={chunks[chunk_id]['token_count']})"
        )

        try:
            response, elapsed = call_model(
                client=client,
                payload=payload,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
        except Exception as exc:
            logger.error(f"  Errore API: {exc}")
            per_chunk_log.append({
                "chunk_id": chunk_id,
                "status": "api_error",
                "error": repr(exc),
            })
            continue

        try:
            flat = extract_tool_use(response)
        except RuntimeError as exc:
            logger.error(f"  {exc}")
            per_chunk_log.append({
                "chunk_id": chunk_id,
                "status": "no_tool_use",
                "error": str(exc),
                "stop_reason": getattr(response, "stop_reason", None),
            })
            continue

        try:
            graph, invalid_edges = build_extracted_graph(
                flat=flat,
                model=args.model,
                timestamp=datetime.now(timezone.utc),
            )
        except ValidationError as exc:
            logger.error(f"  ValidationError: {exc}")
            per_chunk_log.append({
                "chunk_id": chunk_id,
                "status": "validation_error",
                "error": str(exc),
            })
            continue

        # model_dump(mode="json") serializza datetime/enum in formati JSON-friendly
        extractions.append(graph.model_dump(mode="json"))

        usage = response.usage
        log_entry = {
            "chunk_id": chunk_id,
            "status": "ok",
            "elapsed_s": round(elapsed, 2),
            "tokens_input": usage.input_tokens,
            "tokens_output": usage.output_tokens,
            "tokens_cache_creation": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "tokens_cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "n_nodes": len(graph.nodes),
            "n_edges": len(graph.edges),
            "n_invalid_edges": len(invalid_edges),
            "invalid_edges": invalid_edges,
        }
        per_chunk_log.append(log_entry)

        logger.info(
            f"  -> {log_entry['n_nodes']} nodi, {log_entry['n_edges']} archi"
            + (f" (+{log_entry['n_invalid_edges']} scartati)" if invalid_edges else "")
            + f", {elapsed:.1f}s, "
            f"in={log_entry['tokens_input']} out={log_entry['tokens_output']} "
            f"cache_w={log_entry['tokens_cache_creation']} "
            f"cache_r={log_entry['tokens_cache_read']}"
        )

        # Persistenza incrementale: l'output è sempre in stato coerente
        write_atomic_json(args.output, build_output_envelope(extractions, args, started_at))

    finished_at = datetime.now(timezone.utc)
    log_envelope = {
        "stage_version": STAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": prompt_mod.PROMPT_VERSION,
        "model": args.model,
        "params": {
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "remove_description": prompt_mod.REMOVE_DESCRIPTION,
        },
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "n_chunks_requested": len(selected),
        "totals": aggregate_totals(per_chunk_log),
        "per_chunk": per_chunk_log,
    }
    write_atomic_json(args.log, log_envelope)

    logger.info(f"Done. Output -> {args.output}")
    logger.info(f"      Log    -> {args.log}")
    logger.info(f"      Totali: {log_envelope['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
