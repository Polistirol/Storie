#!/usr/bin/env python
"""
src/stage_3-2_extract.py
Stage 3.2 — estrazione knowledge graph chunk per chunk.

Legge i chunk da un file `chunks.json` (default `data/stage_2/chunks.json`),
per ognuno chiama Claude con il prompt e gli esempi few-shot definiti in
`stage_3-1_prompt.py`, raccoglie l'output del tool `submit_extraction`,
arricchisce con `Provenance`, valida con i modelli Pydantic di `schema.py`,
scrive i grafi estratti in `extracted_graph.json` (`{"extractions": [...]}`)
e i metadati di run + log dettagliato in `extraction_log.json`.

===============================================================================
QUICK REFERENCE — flag e modalità
===============================================================================

Selezione chunk (mutuamente esclusivi):
  (nessuno)                    Smoke test sul set annotato a mano in
                               `data/stage_3/test/` (Pila B). Default sicuro.
  --test-dir DIR               Estrae i chunk i cui file `ch_*.json` sono in
                               DIR (testo da `chunks.json`); scrive
                               `extracted_graph_test.json` e
                               `extraction_log_test.json` in DIR. Sync only.
  --chunks ch_0001 ch_0047 ... Lista esplicita di chunk_id. Utile per debug
                               mirato di pochi chunk.
  --all                        Tutti i chunk del file chunks.json di default
                               (`data/stage_2/chunks.json`).
  --full-run CHUNKS_FILE OUT   Tutti i chunk del file CHUNKS_FILE; gli
                               output finiscono in
                               OUT/dd-mm-yyyy_HH-MM/ (subcartella nuova
                               ad ogni lancio, riusata solo durante il
                               resume di un batch in corso). Override
                               possibile via --output/--log. Chiede
                               conferma interattiva prima di partire
                               (warning grosso se manca --batch).
                               Pensato per le run massicce.

Modalità di chiamata:
  (default)                    SYNC: una chiamata per chunk, in sequenza,
                               persistenza incrementale dopo ogni chunk.
                               Pieno costo input/output token.
  --batch                      BATCH: Message Batches API di Anthropic.
                               50% di sconto sui token, asincrono, una sola
                               sottomissione + polling. Richiede --full-run
                               (o --resume-batch). Output identico al sync.
  --resume-batch BATCH_ID      Riprende un batch già sottomesso (skip create,
                               solo polling + collect). Implica --batch.
                               Utile se hai chiuso il terminale durante
                               l'attesa. Il batch_id lo trovi anche in
                               batches/batch_state.json o nei log Anthropic.

Parametri modello:
  --model MODEL                Default: claude-sonnet-4-6 (ADR-010).
  --max-tokens N               Default: 16000. È solo un cap, non costa
                               nulla alzarlo. Sotto i 64k di Sonnet 4.6.
  --temperature T              Default: 0.0 (deterministico).

I/O e controllo:
  --output PATH                File di output cumulativo (override di
                               default e di OUT/extracted_graph.json).
  --log PATH                   File di log dettagliato (override di default
                               e di OUT/extraction_log.json).
  --dry-run                    Stampa il payload del primo chunk e termina,
                               senza chiamare l'API. Per ispezionare prompt
                               e few-shot effettivi.
  --skip-existing              Se l'output esiste già, salta i chunk già
                               estratti (solo modalità sync).
  --yes, -y                    Salta la conferma interattiva di --full-run.
  -v, --verbose                Log livello DEBUG.

===============================================================================
ESEMPI D'USO
===============================================================================

# 1. Smoke test sui chunk di Pila B in una cartella test (sync + cache)
python src/stage_3-2_extract.py --test-dir data/stage_3/test

# 1b. Default senza flag: stessi chunk di Pila B, output in data/stage_3/
python src/stage_3-2_extract.py \\
    --output data/stage_3/extracted_graph_test.json \\
    --log data/stage_3/extraction_log_test.json

# 2. Debug mirato su uno o pochi chunk specifici
python src/stage_3-2_extract.py --chunks ch_0001 ch_0047

# 3. Ispezione del payload senza chiamare l'API (verifica prompt + few-shot)
python src/stage_3-2_extract.py --chunks ch_0001 --dry-run

# 4. FULL RUN consigliata: tutti i 310 chunk in BATCH (50% sconto, async)
python src/stage_3-2_extract.py \\
    --full-run data/stage_2/chunks.json data/stage_3/full_run/ \\
    --batch

# 5. Ripresa di un batch in corso dopo aver chiuso il terminale.
#    Stesso comando di prima riusa batches/batch_state.json automaticamente:
python src/stage_3-2_extract.py \\
    --full-run data/stage_2/chunks.json data/stage_3/full_run/ \\
    --batch
# In alternativa, batch_id esplicito:
python src/stage_3-2_extract.py \\
    --full-run data/stage_2/chunks.json data/stage_3/full_run/ \\
    --resume-batch msgbatch_01ABCxyz...

# 6. Full run SYNC (sconsigliata per volumi alti: tariffa piena, sequenziale).
#    Il comando mostra un warning grosso prima di partire.
python src/stage_3-2_extract.py \\
    --full-run data/stage_2/chunks.json data/stage_3/full_run/

# 7. Full run da script/CI (salta la conferma interattiva)
python src/stage_3-2_extract.py \\
    --full-run data/stage_2/chunks.json data/stage_3/full_run/ \\
    --batch --yes

# 8. Tutti i chunk del default chunks.json, sync (utile per piccoli corpus)
python src/stage_3-2_extract.py --all

# 9. Sync con resume di una run interrotta (riusa OUT esistente e salta i
#    chunk già estratti). Funziona SOLO in sync, non in batch.
python src/stage_3-2_extract.py --all --skip-existing

===============================================================================
FORMATO DI OUTPUT
===============================================================================

Stesso schema in sync e in batch (il batch cambia solo il trasporto):

  extracted_graph.json -> { extractions: [ExtractedGraph...] }
                          (solo i grafi estratti, niente metadati di run)

  extraction_log.json  -> envelope { source, created_at, stage_version,
                                     schema_version, prompt_version, model,
                                     params, mode: "sync"|"batch",
                                     started_at, finished_at,
                                     n_chunks_requested,
                                     total_chunks_processed,
                                     totals: {...}, per_chunk: [...],
                                     batch?: { batch_id, request_counts, ... } }

Layout in modalità --full-run:

  batches/                                 <- Adriano_graph/batches/
    batch_state.json                       <- (solo batch in corso) state
                                              per il resume automatico:
                                              { batch_id, model, n_requests,
                                                output_subdir, output_path,
                                                selected_chunk_ids, ... }
    batch_state_msgbatch_xxx.json          <- (solo batch terminato) state
                                              archiviato dopo l'`ended`,
                                              tenuto per archeologia.

  OUTPUT_DIR/                              <- passata da --full-run
    dd-mm-yyyy_HH-MM/                      <- subcartella della run
      extracted_graph.json                    (creata nuova ad ogni
      extraction_log.json                     full-run, riusata solo
                                              durante il resume di un
                                              batch in corso).

Sul timestamp: dd-mm-yyyy_HH-MM (es. 18-05-2026_16-45). Uso `-` invece di
`:` perché su Windows i `:` non sono ammessi nei nomi file. Su collisione
(due full-run nello stesso minuto) la seconda diventa `..._HH-MM_2`, ecc.

Resume del batch: rilanciare lo stesso comando `--full-run` mentre
batch_state.json esiste fa ripartire il polling sullo stesso batch e
scrive gli output nella stessa subcartella. A batch terminato lo state
viene archiviato automaticamente, così il lancio successivo crea una
run nuova invece di tentare un resume del batch già finito.

===============================================================================
NOTE OPERATIVE
===============================================================================

- In modalità sync le chiamate sono sequenziali per debug e per massimizzare
  l'hit rate del prompt caching (ADR-013): chiamate parallele potrebbero
  servire la cache prima che il primo write sia ack'd. La cache breakpoint
  usa TTL 1h, sufficiente a coprire l'intera run su 310 chunk.
- In modalità batch la concorrenza la gestisce Anthropic e il caching è
  best-effort (vedi doc Message Batches API). Il TTL 1h aiuta a mantenere
  cache hit anche quando il batch si distende su molti minuti.
- Limiti batch Anthropic: 100k request / 256 MB di payload per batch,
  TTL 24h. La full run su 310 chunk è ampiamente dentro questi limiti.
- Auth: legge `ANTHROPIC_API_KEY` da `.env` (alla root del repo) o
  dall'environment.

===============================================================================
NB sui nomi file
===============================================================================

Questo script importa `stage_3-1_prompt.py`, che ha un trattino nel nome
e quindi NON è importabile con `import` standard. Uso
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
# `max_tokens` è solo un cap (l'API fattura i token effettivamente prodotti):
# alzarlo non costa nulla, taglia solo quando il modello vorrebbe scrivere di
# più. Worst case osservato sui chunk di test = ~5300 tok output (chunk
# densi con ~20 nodi + ~20 archi e description corpose); 16k dà ~3x di
# margine, ben sotto i 64k max output di Sonnet 4.6.
# Se max_tokens viene raggiunto, `stop_reason == "max_tokens"`: il tool_use
# arriva troncato → `no_tool_use` / `validation_error` nel log E **i token
# output prodotti vengono comunque fatturati**, quindi conviene un cap
# largo.
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TEMPERATURE = 0.0

CHUNKS_PATH = PROJECT_ROOT / "data" / "stage_2" / "chunks.json"
BATCHES_DIR = PROJECT_ROOT / "batches"
TEST_DIR = PROJECT_ROOT / "data" / "stage_3" / "test"
SUBJECT_PROFILE_PATH = PROJECT_ROOT / "data" / "subject_profile_adriano.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "stage_3" / "extracted_graph.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "data" / "stage_3" / "extraction_log.json"
TEST_OUTPUT_NAME = "extracted_graph_test.json"
TEST_LOG_NAME = "extraction_log_test.json"

# Polling del Message Batches API: 24h è il TTL massimo lato Anthropic.
BATCH_POLL_INTERVAL_S = 30
BATCH_TIMEOUT_S = 24 * 3600


logger = logging.getLogger("stage_3_extract")


# -----------------------------------------------------------------------------
# Caricamento e selezione
# -----------------------------------------------------------------------------

def load_chunks(path: Path | str | None = None) -> dict[str, dict]:
    """Indice chunk_id -> chunk completo dal file `chunks.json`.

    Se `path` è None, usa `CHUNKS_PATH` di default. Altrimenti carica dal file
    indicato (utile per `--full-run` quando i chunk vivono altrove, es. testi
    diversi della pipeline clinica).
    """
    chunks_path = Path(path) if path is not None else CHUNKS_PATH
    with chunks_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {c["chunk_id"]: c for c in data["chunks"]}


def load_subject_profile_text(path: Path | None = None) -> str:
    """Carica il profilo soggetto da JSON e lo renderizza per il SYSTEM_PROMPT."""
    profile_path = path or SUBJECT_PROFILE_PATH
    with profile_path.open("r", encoding="utf-8") as f:
        profile = json.load(f)
    return prompt_mod.render_subject_profile(profile)


def list_test_dir_chunk_ids(test_dir: Path) -> list[str]:
    """Ritorna i chunk_id (stem di `ch_*.json`) presenti in una cartella test."""
    return sorted(p.stem for p in test_dir.glob("ch_*.json"))


def resolve_chunk_selection(
    args: argparse.Namespace,
    chunks: dict[str, dict],
) -> list[str]:
    """Ritorna la lista ordinata di chunk_id da processare in base agli argomenti.

    `chunks` è l'indice già caricato (per `--full-run` può essere un file
    diverso da `CHUNKS_PATH`): viene usato per le selezioni che operano
    sull'intero corpus (`--all`, `--full-run`).
    """
    if args.chunks:
        return list(args.chunks)

    if args.test_dir:
        return list_test_dir_chunk_ids(Path(args.test_dir))

    if args.full_run or args.all:
        # ordine naturale (zero-padded) garantisce idempotenza nei retry
        return sorted(chunks.keys())

    # Default: chunks annotati a mano in data/stage_3/test/ (Pila B)
    return list_test_dir_chunk_ids(TEST_DIR)


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
            role=e.get("role"),
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


def normalize_flat_extraction(flat: dict) -> dict:
    """Normalizza la shape flat dal tool_use prima di `build_extracted_graph`.

    In rari casi il modello passa `nodes` o `edges` come stringa JSON invece
    che come array; proviamo a decodificarla. Solleva `ValueError` se la shape
    resta incompatibile (es. JSON troncato o malformato).
    """
    out = dict(flat)
    for key in ("nodes", "edges"):
        val = out.get(key, [])
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{key} è una stringa ma non è JSON valido: {exc}"
                ) from exc
            out[key] = val
        if not isinstance(out.get(key), list):
            raise ValueError(
                f"{key} deve essere una lista, ricevuto {type(out.get(key)).__name__}"
            )
    for i, n in enumerate(out.get("nodes", [])):
        if not isinstance(n, dict):
            raise ValueError(
                f"nodes[{i}] deve essere un oggetto, ricevuto {type(n).__name__}: {n!r}"
            )
    for i, e in enumerate(out.get("edges", [])):
        if not isinstance(e, dict):
            raise ValueError(
                f"edges[{i}] deve essere un oggetto, ricevuto {type(e).__name__}: {e!r}"
            )
    return out


def process_response(
    response: Any,
    chunk_id: str,
    model: str,
    elapsed: float | None = None,
) -> tuple[dict | None, dict]:
    """Trasforma una `Message` Anthropic in `(extraction_dump, log_entry)`.

    Funzione comune al flusso sincrono e a quello batch: prende la stessa
    shape di `Message`, ne estrae il `tool_use` `submit_extraction`, valida
    via Pydantic e ricostruisce la `Provenance`.

    Ritorna `(None, log_entry)` in caso di errore (no tool_use o validation
    error). Altrimenti `(extraction_dump, log_entry)` con `extraction_dump`
    già serializzato via `model_dump(mode="json")` e pronto per finire dentro
    l'envelope di output.

    `elapsed` è il tempo di chiamata in secondi (solo per il flusso sync;
    nel batch non ha significato e va lasciato a None).
    """
    try:
        flat = normalize_flat_extraction(extract_tool_use(response))
    except RuntimeError as exc:
        return None, {
            "chunk_id": chunk_id,
            "status": "no_tool_use",
            "error": str(exc),
            "stop_reason": getattr(response, "stop_reason", None),
        }
    except (TypeError, KeyError, ValueError) as exc:
        return None, {
            "chunk_id": chunk_id,
            "status": "validation_error",
            "error": str(exc),
        }

    try:
        graph, invalid_edges = build_extracted_graph(
            flat=flat,
            model=model,
            timestamp=datetime.now(timezone.utc),
        )
    except (ValidationError, TypeError, KeyError, ValueError) as exc:
        return None, {
            "chunk_id": chunk_id,
            "status": "validation_error",
            "error": str(exc),
        }

    usage = response.usage
    log_entry: dict[str, Any] = {
        "chunk_id": chunk_id,
        "status": "ok",
        "tokens_input": usage.input_tokens,
        "tokens_output": usage.output_tokens,
        "tokens_cache_creation": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "tokens_cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "n_nodes": len(graph.nodes),
        "n_edges": len(graph.edges),
        "n_invalid_edges": len(invalid_edges),
        "invalid_edges": invalid_edges,
    }
    if elapsed is not None:
        log_entry["elapsed_s"] = round(elapsed, 2)

    return graph.model_dump(mode="json"), log_entry


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

def resolve_chunks_source(chunks_source_path: Path | None) -> str:
    """Path del file chunks per i metadati di log (preferibilmente relativo al progetto)."""
    path = chunks_source_path or CHUNKS_PATH
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def build_output_payload(extractions: list[dict]) -> dict:
    """Payload scritto in `extracted_graph.json`: solo i grafi estratti."""
    return {"extractions": extractions}


def build_log_envelope(
    extractions: list[dict],
    args: argparse.Namespace,
    started_at: datetime,
    finished_at: datetime,
    selected: list[str],
    per_chunk_log: list[dict],
    chunks_source: str | None = None,
    batch_meta: dict | None = None,
) -> dict:
    """Envelope scritto in `extraction_log.json`: metadati di run + log per-chunk."""
    envelope: dict[str, Any] = {
        "source": chunks_source or "data/stage_2/chunks.json",
        "created_at": started_at.isoformat(),
        "stage_version": STAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": prompt_mod.PROMPT_VERSION,
        "model": args.model,
        "params": {
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "remove_description": prompt_mod.REMOVE_DESCRIPTION,
        },
        "mode": "batch" if args.batch else "sync",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "n_chunks_requested": len(selected),
        "total_chunks_processed": len(extractions),
        "totals": aggregate_totals(per_chunk_log),
        "per_chunk": per_chunk_log,
    }
    if batch_meta is not None:
        envelope["batch"] = batch_meta
    return envelope


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
# Conferma interattiva + warning per full-run
# -----------------------------------------------------------------------------

def confirm_full_run(
    n_chunks: int,
    output_path: Path,
    log_path: Path,
    use_batch: bool,
    auto_yes: bool,
) -> bool:
    """Stampa un riepilogo della full-run e chiede conferma all'utente.

    Se `use_batch=False` mostra un warning ben visibile: il sync mode su
    centinaia di chunk significa centinaia di chiamate sequenziali a tariffa
    piena, mentre il batch ha 50% di sconto e gestione asincrona.

    `auto_yes=True` salta l'`input()` (utile per CI / lanci da script).
    Ritorna True se si può procedere, False se l'utente ha annullato.
    """
    sep = "=" * 72
    print(sep)
    print(f"FULL RUN — estrazione su {n_chunks} chunk")
    print(sep)
    print(f"  output       : {output_path}")
    print(f"  log          : {log_path}")
    print(f"  mode         : {'BATCH (50% sconto, asincrono)' if use_batch else 'SYNC (sequenziale, tariffa piena)'}")
    if not use_batch:
        print()
        print("  !!! WARNING !!!")
        print("  Stai per fare una FULL RUN in modalità SYNC, senza --batch.")
        print("  Significa N chiamate API sequenziali a TARIFFA PIENA.")
        print("  Per una run massiva ti conviene usare --batch:")
        print("    - 50% di sconto sul prezzo dei token,")
        print("    - una sola sottomissione invece di centinaia di round-trip,")
        print("    - stesso formato di output finale.")
        print("  Procedi solo se sai cosa stai facendo (es. debug di pochi chunk).")
    print(sep)

    if auto_yes:
        print("  --yes attivo: salto la conferma interattiva.")
        return True

    try:
        answer = input("Procedo? [y/N]: ").strip().lower()
    except EOFError:
        # stdin non disponibile (es. pipe non interattiva): nego per sicurezza
        logger.error("stdin non interattivo: usa --yes per saltare la conferma.")
        return False
    return answer in ("y", "yes", "s", "si", "sì")


# -----------------------------------------------------------------------------
# Message Batches API
#
# Anthropic Messages Batches: 50% sconto sui token, asincrono, fino a 100k
# request per batch (o 256 MB di payload), TTL 24h.
# Doc: https://docs.claude.com/en/docs/build-with-claude/batch-processing
#
# Workflow:
#   1. submit_batch(): client.messages.batches.create(requests=[...])
#   2. poll_batch():   loop su client.messages.batches.retrieve(batch_id)
#                      finché processing_status == "ended".
#   3. collect_batch_results(): stream da client.messages.batches.results(batch_id),
#                      raccoglie per custom_id (che coincide col chunk_id).
#
# I `result.message` restituiti hanno la stessa shape di una Message normale,
# quindi `process_response` li gestisce identicamente al flusso sync.
# -----------------------------------------------------------------------------

def submit_batch(
    client: anthropic.Anthropic,
    selected: list[str],
    chunks: dict[str, dict],
    args: argparse.Namespace,
) -> Any:
    """Crea il batch con una request per chunk e ritorna l'oggetto batch."""
    # Import locali: l'SDK espone questi tipi in moduli dedicati. Restano
    # opzionali per gli utenti che usano solo il flusso sync (vecchie versioni
    # dell'SDK potrebbero non averli).
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests: list[Request] = []
    for chunk_id in selected:
        chunk_text = chunks[chunk_id]["text"]
        payload = prompt_mod.build_request_payload(
            chunk_id=chunk_id,
            chunk_text=chunk_text,
            model=args.model,
            subject_profile=args._subject_profile,
        )
        # custom_id deve matchare ^[a-zA-Z0-9_-]{1,64}$: i nostri chunk_id
        # ("ch_0001" ecc.) sono già conformi.
        requests.append(Request(
            custom_id=chunk_id,
            params=MessageCreateParamsNonStreaming(
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                **payload,
            ),
        ))

    return client.messages.batches.create(requests=requests)


def poll_batch(
    client: anthropic.Anthropic,
    batch_id: str,
    poll_interval_s: int = BATCH_POLL_INTERVAL_S,
    timeout_s: int = BATCH_TIMEOUT_S,
) -> Any:
    """Polla finché lo stato del batch diventa `ended` (o timeout).

    Logga conteggi parziali ogni poll. `ended` include sia il caso di
    successo completo sia il caso "ended con errori parziali": entrambi
    hanno risultati scaricabili.
    """
    elapsed = 0
    while elapsed < timeout_s:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        logger.info(
            f"Batch {batch_id} status={batch.processing_status} "
            f"counts: processing={counts.processing} succeeded={counts.succeeded} "
            f"errored={counts.errored} canceled={counts.canceled} expired={counts.expired}"
        )
        if batch.processing_status == "ended":
            return batch
        time.sleep(poll_interval_s)
        elapsed += poll_interval_s

    raise RuntimeError(
        f"Timeout dopo {timeout_s}s aspettando il batch {batch_id}. "
        f"Stato corrente: {batch.processing_status}. "
        f"Puoi riprenderlo con --resume-batch {batch_id}."
    )


def collect_batch_results(
    client: anthropic.Anthropic,
    batch_id: str,
    selected: list[str],
    model: str,
) -> tuple[list[dict], list[dict]]:
    """Stream dei risultati del batch e produce `(extractions, per_chunk_log)`.

    I risultati arrivano in ordine arbitrario: usiamo `custom_id` per il
    match e ricostruiamo l'ordine canonico (zero-padded) di `selected` alla
    fine, così l'output resta deterministico run-by-run.
    """
    extractions_by_id: dict[str, dict] = {}
    log_by_id: dict[str, dict] = {}

    seen_ids: set[str] = set()
    for r in client.messages.batches.results(batch_id):
        cid = r.custom_id
        seen_ids.add(cid)
        result_type = r.result.type

        if result_type == "succeeded":
            extraction, log_entry = process_response(
                response=r.result.message,
                chunk_id=cid,
                model=model,
            )
            if extraction is not None:
                extractions_by_id[cid] = extraction
            log_by_id[cid] = log_entry

        elif result_type == "errored":
            err = getattr(r.result, "error", None)
            err_inner = getattr(err, "error", None)
            log_by_id[cid] = {
                "chunk_id": cid,
                "status": "api_error",
                "error_type": getattr(err_inner, "type", None),
                "error": getattr(err_inner, "message", repr(err)),
            }

        elif result_type == "canceled":
            log_by_id[cid] = {"chunk_id": cid, "status": "canceled"}

        elif result_type == "expired":
            log_by_id[cid] = {"chunk_id": cid, "status": "expired"}

        else:
            log_by_id[cid] = {
                "chunk_id": cid,
                "status": "unknown_result_type",
                "result_type": result_type,
            }

    # Chunk presenti nella selezione ma assenti dai risultati (non dovrebbe
    # mai succedere con un batch `ended`, ma copriamo il caso per non perdere
    # silenziosamente nulla nel log).
    for cid in selected:
        if cid not in seen_ids:
            log_by_id[cid] = {
                "chunk_id": cid,
                "status": "missing_from_batch_results",
            }

    extractions = [extractions_by_id[cid] for cid in selected if cid in extractions_by_id]
    per_chunk_log = [log_by_id[cid] for cid in selected if cid in log_by_id]
    return extractions, per_chunk_log


def run_batch_mode(
    client: anthropic.Anthropic,
    selected: list[str],
    chunks: dict[str, dict],
    args: argparse.Namespace,
) -> tuple[list[dict], list[dict], dict]:
    """Orchestratore del flusso batch.

    Logica di resume: se l'utente passa `--resume-batch BATCH_ID` lo usiamo.
    Altrimenti, se esiste già un `batch_state.json` (residuo di una run
    interrotta) lo riusiamo, così rilanciare lo stesso comando dopo la
    chiusura del terminale fa polling sul batch esistente invece di
    crearne uno nuovo.

    Lo state vive in `batches/` (non nella OUTPUT_DIR né nella subcartella
    timestampata), così è condiviso fra rilanci. Path tracciato in
    `args._batch_state_path` da `main()`. Fallback: `batches/batch_state.json`.

    Ritorna `(extractions, per_chunk_log, batch_meta)`. `batch_meta` viene
    iniettato nell'envelope del log per tracciabilità.
    """
    output_path = Path(args.output)
    state_path: Path = (
        getattr(args, "_batch_state_path", None)
        or BATCHES_DIR / "batch_state.json"
    )
    out_subdir: Path | None = getattr(args, "_out_subdir", None)

    if args.resume_batch:
        batch_id = args.resume_batch
        logger.info(f"Resume esplicito da --resume-batch: {batch_id}")
    elif state_path.exists():
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        batch_id = state["batch_id"]
        logger.info(
            f"Trovato batch_state.json in {state_path}: riprendo batch_id={batch_id}. "
            f"Per forzare un nuovo batch elimina il file."
        )
    else:
        logger.info(f"Sottometto un nuovo batch con {len(selected)} request...")
        batch = submit_batch(client, selected, chunks, args)
        batch_id = batch.id
        logger.info(f"Batch creato: id={batch_id} created_at={getattr(batch, 'created_at', None)}")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_json(state_path, {
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "n_requests": len(selected),
            "chunks_source": str(args.full_run[0]) if args.full_run else None,
            "output_subdir": str(out_subdir) if out_subdir is not None else None,
            "output_path": str(output_path),
            "selected_chunk_ids": selected,
        })

    batch = poll_batch(client, batch_id)
    logger.info("Batch ended. Recupero risultati...")
    extractions, per_chunk_log = collect_batch_results(client, batch_id, selected, args.model)

    batch_meta = {
        "batch_id": batch_id,
        "processing_status": getattr(batch, "processing_status", None),
        "created_at": (batch.created_at.isoformat()
                       if getattr(batch, "created_at", None) is not None else None),
        "ended_at": (batch.ended_at.isoformat()
                     if getattr(batch, "ended_at", None) is not None else None),
        "request_counts": {
            "processing": batch.request_counts.processing,
            "succeeded": batch.request_counts.succeeded,
            "errored": batch.request_counts.errored,
            "canceled": batch.request_counts.canceled,
            "expired": batch.request_counts.expired,
        } if getattr(batch, "request_counts", None) is not None else None,
    }

    # Archivio lo state: un batch `ended` non si "riprende" più, e lasciare
    # batch_state.json in piedi farebbe sì che il prossimo lancio dello
    # stesso comando ri-pollasse uno stato già completato invece di
    # sottomettere un nuovo batch. Rinomino in batch_state_<batch_id>.json
    # per archeologia. Se rinominato esiste già (raro), sovrascrivo.
    if getattr(batch, "processing_status", None) == "ended" and state_path.exists():
        archived = state_path.parent / f"batch_state_{batch_id}.json"
        try:
            if archived.exists():
                archived.unlink()
            state_path.rename(archived)
            logger.info(f"State archiviato in {archived}")
        except OSError as exc:
            logger.warning(f"Impossibile archiviare batch_state.json ({exc}).")

    return extractions, per_chunk_log, batch_meta


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run_sync_mode(
    client: anthropic.Anthropic,
    selected: list[str],
    chunks: dict[str, dict],
    args: argparse.Namespace,
    started_at: datetime,
    existing_extractions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Flusso classico: una chiamata per chunk, in sequenza, con persistenza
    incrementale dopo ogni chunk per essere robusti a interruzioni.
    """
    extractions: list[dict] = list(existing_extractions)
    per_chunk_log: list[dict] = []

    for i, chunk_id in enumerate(selected, start=1):
        chunk_text = chunks[chunk_id]["text"]
        payload = prompt_mod.build_request_payload(
            chunk_id=chunk_id,
            chunk_text=chunk_text,
            model=args.model,
            subject_profile=args._subject_profile,
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

        extraction, log_entry = process_response(
            response=response,
            chunk_id=chunk_id,
            model=args.model,
            elapsed=elapsed,
        )
        per_chunk_log.append(log_entry)

        if extraction is None:
            logger.error(f"  {log_entry.get('status')}: {log_entry.get('error')}")
            continue

        extractions.append(extraction)
        logger.info(
            f"  -> {log_entry['n_nodes']} nodi, {log_entry['n_edges']} archi"
            + (f" (+{log_entry['n_invalid_edges']} scartati)" if log_entry['n_invalid_edges'] else "")
            + f", {elapsed:.1f}s, "
            f"in={log_entry['tokens_input']} out={log_entry['tokens_output']} "
            f"cache_w={log_entry['tokens_cache_creation']} "
            f"cache_r={log_entry['tokens_cache_read']}"
        )

        # Persistenza incrementale: l'output è sempre in stato coerente
        write_atomic_json(args.output, build_output_payload(extractions))

    return extractions, per_chunk_log


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3.2 — extraction")

    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--chunks", nargs="+", metavar="CHUNK_ID",
                     help="Lista esplicita di chunk_id (es. ch_0001 ch_0047)")
    sel.add_argument("--all", action="store_true",
                     help="Processa tutti i chunk presenti in chunks.json (default)")
    sel.add_argument("--full-run", nargs=2, metavar=("CHUNKS_FILE", "OUTPUT_DIR"),
                     help=(
                         "Full run su tutti i chunk del file CHUNKS_FILE, "
                         "salvando estrazione e log in OUTPUT_DIR. "
                         "Pensata per le run massicce: usare insieme a --batch "
                         "per ottenere il 50%% di sconto sui token via "
                         "Message Batches API."
                     ))
    sel.add_argument("--test-dir", metavar="DIR",
                     help=(
                         "Estrae i chunk con file ch_*.json in DIR (testo da "
                         "chunks.json); scrive extracted_graph_test.json e "
                         "extraction_log_test.json in DIR. Solo sync (no --batch)."
                     ))
    # Default (nessun flag): test set in data/stage_3/test/, Pila B

    parser.add_argument("--batch", action="store_true",
                        help=(
                            "Usa la Message Batches API di Anthropic (asincrona, "
                            "50%% di sconto). Valido solo insieme a --full-run."
                        ))
    parser.add_argument("--resume-batch", metavar="BATCH_ID",
                        help=(
                            "Riprende un batch già sottomesso: salta create, "
                            "fa solo polling + collect dei risultati. "
                            "Implica --batch e --full-run."
                        ))
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Salta la conferma interattiva di --full-run.")

    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Modello Anthropic (default: {DEFAULT_MODEL}, da ADR-010)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH),
                        help="File di output cumulativo (override anche con --full-run)")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH),
                        help="File di log dettagliato (override anche con --full-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stampa il payload del primo chunk e termina, senza chiamare l'API")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Se l'output esiste già, salta i chunk già estratti (solo sync)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # --resume-batch implica --batch e --full-run. Se l'utente passa
    # solo --resume-batch, attiviamo i flag impliciti senza forzarlo a
    # rispecificarli (ma teniamo args.full_run = None se non lo ha messo:
    # in resume non rilanciamo il submit, quindi CHUNKS_FILE non serve).
    if args.resume_batch:
        args.batch = True

    # --batch ha senso solo con --full-run (la batch API serve per volumi).
    if args.batch and not args.full_run and not args.resume_batch:
        logger.error("--batch richiede --full-run (o --resume-batch).")
        return 1

    if args.test_dir and args.batch:
        logger.error("--batch non è compatibile con --test-dir (usa sync).")
        return 1

    if args.test_dir:
        test_dir = Path(args.test_dir)
        if not test_dir.is_dir():
            logger.error(f"--test-dir: cartella non trovata: {test_dir}")
            return 1
        chunk_files = list(test_dir.glob("ch_*.json"))
        if not chunk_files:
            logger.error(
                f"--test-dir: nessun file ch_*.json in {test_dir.resolve()}"
            )
            return 1
        if args.output == str(DEFAULT_OUTPUT_PATH):
            args.output = str(test_dir / TEST_OUTPUT_NAME)
        if args.log == str(DEFAULT_LOG_PATH):
            args.log = str(test_dir / TEST_LOG_NAME)

    # --full-run override di --output, --log e del file chunks.
    #
    # Layout fisico delle cartelle, dato `--full-run CHUNKS_FILE OUTPUT_DIR/`:
    #
    #   batches/
    #     batch_state.json                       <- (solo batch) state per il
    #                                               resume, condiviso fra
    #                                               rilanci dello stesso
    #                                               comando.
    #     batch_state_msgbatch_xxx.json          <- (solo batch) state
    #                                               archiviato dopo che il
    #                                               batch è andato in ended,
    #                                               tenuto per archeologia.
    #
    #   OUTPUT_DIR/
    #     dd-mm-yyyy_HH-MM/                      <- subcartella per la run
    #       extracted_graph.json                    corrente, NUOVA ad ogni
    #       extraction_log.json                     full-run a meno che non
    #                                               stiamo riprendendo un
    #                                               batch in corso.
    #
    # Note:
    # - Il formato del timestamp usa `-` invece di `:` perché su Windows
    #   `:` non è ammesso nei nomi file (è riservato per i drive letter).
    # - In sync ogni rilancio crea una nuova subdir. Se vuoi riprendere
    #   un sync interrotto passa `--output <subdir>/extracted_graph.json`
    #   esplicito + `--skip-existing`.
    chunks_source_path: Path | None = None
    if args.full_run:
        chunks_file, output_dir = args.full_run
        chunks_source_path = Path(chunks_file)
        if not chunks_source_path.exists():
            logger.error(f"--full-run: file chunk non trovato: {chunks_source_path}")
            return 1

        out_root = Path(output_dir)
        out_root.mkdir(parents=True, exist_ok=True)

        # State del batch vive in batches/ (fuori da OUTPUT_DIR), così lo
        # stesso comando rilanciato può ritrovarlo e riprendere senza
        # ri-sottomettere il batch.
        BATCHES_DIR.mkdir(parents=True, exist_ok=True)
        state_path = BATCHES_DIR / "batch_state.json"

        # Decidi se creare una NUOVA subcartella timestampata o riusarne una
        # esistente referenziata dallo state. Riuso solo in batch e solo se
        # lo state ha un `output_subdir` ancora esistente sul filesystem.
        out_subdir: Path | None = None
        if (args.batch or args.resume_batch) and state_path.exists():
            try:
                with state_path.open("r", encoding="utf-8") as f:
                    saved_state = json.load(f)
                saved = saved_state.get("output_subdir")
                if saved:
                    candidate = Path(saved)
                    # Tollero state salvato sia come path assoluto sia come
                    # nome relativo (es. mossa cartella).
                    for c in (candidate, out_root / candidate.name):
                        if c.exists():
                            out_subdir = c
                            logger.info(f"Riuso subcartella di run esistente: {out_subdir}")
                            break
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"batch_state.json illeggibile ({exc}): ne creo uno nuovo.")

        if out_subdir is None:
            # Formato richiesto: dd-mm-yyyy_HH-MM. I `:` non sono ammessi su
            # Windows nei nomi file, uso `-` come separatore.
            ts = datetime.now().strftime("%d-%m-%Y_%H-%M")
            out_subdir = out_root / ts
            # Collisione (rilancio nello stesso minuto): suffisso _2, _3, ...
            i = 2
            while out_subdir.exists():
                out_subdir = out_root / f"{ts}_{i}"
                i += 1
            out_subdir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Subcartella della run: {out_subdir}")

        # Espongo a run_batch_mode il path dello state e della subdir.
        args._batch_state_path = state_path  # type: ignore[attr-defined]
        args._out_subdir = out_subdir        # type: ignore[attr-defined]

        # Se l'utente NON ha sovrascritto --output/--log, li riconduciamo a
        # out_subdir (NON a out_root): i file dell'estrazione vivono qui.
        if args.output == str(DEFAULT_OUTPUT_PATH):
            args.output = str(out_subdir / "extracted_graph.json")
        if args.log == str(DEFAULT_LOG_PATH):
            args.log = str(out_subdir / "extraction_log.json")

    chunks = load_chunks(chunks_source_path)
    selected = resolve_chunk_selection(args, chunks)

    if not selected:
        logger.error("Nessun chunk selezionato. Vedi --help.")
        return 1

    missing = [c for c in selected if c not in chunks]
    if missing:
        logger.error(f"chunk_id non presenti nel file chunks: {missing}")
        return 1

    try:
        args._subject_profile = load_subject_profile_text()  # type: ignore[attr-defined]
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.error(f"Impossibile caricare subject profile ({SUBJECT_PROFILE_PATH}): {exc}")
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

    # Resume parziale dell'output (skip-existing). Non ha senso in modalità
    # batch: il batch è atomico, riprenderlo via batch_id è un'altra cosa.
    existing_extractions: list[dict] = []
    if args.skip_existing and not args.batch and Path(args.output).exists():
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
            subject_profile=args._subject_profile,
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

    # Conferma interattiva + warning quando si sta lanciando una full-run.
    # NB: in resume-batch saltiamo, l'utente sta solo recuperando risultati.
    if args.full_run and not args.resume_batch:
        if not confirm_full_run(
            n_chunks=len(selected),
            output_path=Path(args.output),
            log_path=Path(args.log),
            use_batch=args.batch,
            auto_yes=args.yes,
        ):
            logger.info("Annullato dall'utente.")
            return 1

    client = anthropic.Anthropic()  # legge ANTHROPIC_API_KEY dall'env

    started_at = datetime.now(timezone.utc)
    batch_meta: dict | None = None

    if args.batch:
        extractions, per_chunk_log, batch_meta = run_batch_mode(
            client=client,
            selected=selected,
            chunks=chunks,
            args=args,
        )
        # Output cumulativo: stessa shape del flusso sync.
        write_atomic_json(args.output, build_output_payload(extractions))
    else:
        extractions, per_chunk_log = run_sync_mode(
            client=client,
            selected=selected,
            chunks=chunks,
            args=args,
            started_at=started_at,
            existing_extractions=existing_extractions,
        )

    finished_at = datetime.now(timezone.utc)
    log_envelope = build_log_envelope(
        extractions=extractions,
        args=args,
        started_at=started_at,
        finished_at=finished_at,
        selected=selected,
        per_chunk_log=per_chunk_log,
        chunks_source=resolve_chunks_source(chunks_source_path),
        batch_meta=batch_meta,
    )
    write_atomic_json(args.log, log_envelope)

    logger.info(f"Done. Output -> {args.output}")
    logger.info(f"      Log    -> {args.log}")
    logger.info(f"      Totali: {log_envelope['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
