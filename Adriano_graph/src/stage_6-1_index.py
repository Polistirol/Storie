# src/stage_6-1_index.py
"""
================================================================================
STADIO 6 — FASE 1: INDEX (costruzione indice RAG ibrido)
================================================================================

COS'È
  Costruisce gli artefatti di indicizzazione per l'agente conversazionale a
  valle (cartella `inference/`). Lo Stadio 6 è la frontiera fra la pipeline di
  *processing* (stadi 0→5, che producono il grafo) e l'*inferenza* (retrieval +
  LLM, che consuma gli indici). Tutto ciò che è "costruzione di indici" vive
  qui; `inference/` si limita a caricarli e interrogarli (ADR-029).

  Deterministico, zero LLM. Idempotente: a parità di chunk + modello l'output è
  byte-stabile (modulo non-determinismo float della GPU sugli embedding).

COSA PRODUCE (ibrido vettoriale + grafo)
  - Lato VETTORIALE: embedding BGE-M3 dei 310 chunk (lo stesso embedder dello
    stadio 5), salvati come matrice numpy normalizzata + metadati.
  - Lato GRAFO: NON viene duplicato. L'indice registra nel `manifest.json` la
    provenienza del grafo arricchito (path, hash, versioni) contro cui è stato
    costruito; l'inferenza legge il grafo direttamente da `stage_5/` e valida la
    coerenza col manifest (ADR-029, opzione "manifest").

COME SI USA
  python src/stage_6-1_index.py
  python src/stage_6-1_index.py --device cpu
  python src/stage_6-1_index.py --model D:\\models\\bge-m3 --device cuda

INPUT (default)
  data/stage_2/chunks.json                          ← testo dei chunk
  data/stage_5/5_transforms/enriched_graph.json     ← grafo (solo per provenienza)

OUTPUT (cartella data/stage_6/1_index/)
  vectors.npy        matrice (N, D) float32, embedding normalizzati dei chunk
  meta.json          { created_at, model, num_chunks, records[] } — formato
                     consumato da inference/rag/index.py senza modifiche
  chunk_texts.json   { chunk_id: text } — testi in RAM lato inferenza
  manifest.json      provenienza completa: sorgenti (path+sha256+versioni),
                     parametri embed, conteggi. È il contratto build↔inferenza.
  index_log.json     log esplicito: conteggi, parametri, tempi

Vedi ADR-029, PIPELINE.md sezione "Stadio 6 — Index".
================================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STAGE_VERSION = "0.1.0"
DEFAULT_MODEL = r"C:\Users\Pc-Gaming\Documents\models\embeddings\bge-m3"
DEFAULT_DEVICE = "cuda"

_DATA = PROJECT_ROOT / "data"
INPUT_CHUNKS = _DATA / "stage_2" / "chunks.json"
INPUT_GRAPH = _DATA / "stage_5" / "5_transforms" / "enriched_graph.json"
OUT_DIR = _DATA / "stage_6" / "1_index"

VECTORS_FILE = "vectors.npy"
META_FILE = "meta.json"
TEXTS_FILE = "chunk_texts.json"
MANIFEST_FILE = "manifest.json"
LOG_FILE = "index_log.json"


# -----------------------------------------------------------------------------
# IO helpers
# -----------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_chunks(path: Path) -> tuple[list[dict], dict[str, str]]:
    """Ritorna (records, texts) leggendo chunks.json in ordine di file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict] = []
    texts: dict[str, str] = {}
    for chunk in data["chunks"]:
        part = chunk.get("part") or {}
        cid = chunk["chunk_id"]
        records.append(
            {
                "chunk_id": cid,
                "part_number": part.get("number"),
                "part_title": part.get("title"),
                "token_count": chunk.get("token_count"),
            }
        )
        texts[cid] = chunk["text"]
    return records, texts


def _graph_provenance(path: Path) -> dict[str, Any]:
    """Header del grafo + conteggi, senza validazione Pydantic (leggera)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": _rel(path),
        "sha256": _sha256(path),
        "source_run": raw.get("source_run"),
        "source_schema_version": raw.get("source_schema_version"),
        "source_prompt_version": raw.get("source_prompt_version"),
        "dedup_schema_version": raw.get("dedup_schema_version"),
        "stage_version": raw.get("stage_version"),
        "nodes_total": len(raw.get("nodes", [])),
        "edges_total": len(raw.get("edges", [])),
    }


# -----------------------------------------------------------------------------
# Embedding (stesso pattern di stage_5-2a: BGE-M3, normalize_embeddings=True)
# -----------------------------------------------------------------------------

def embed_texts(texts: list[str], model_name: str, device: Optional[str]) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "sentence-transformers non installato. "
            "Installa con: pip install sentence-transformers"
        ) from exc
    print(f"       caricamento modello {model_name} (device={device or 'auto'}) ...")
    model = SentenceTransformer(model_name, device=device)
    print(f"       encoding {len(texts)} chunk ...")
    emb = model.encode(
        texts,
        batch_size=8,
        normalize_embeddings=True,  # coseno = prodotto scalare
        convert_to_numpy=True,
        show_progress_bar=len(texts) > 16,
    )
    return np.asarray(emb, dtype=np.float32)


# -----------------------------------------------------------------------------
# Orchestrazione
# -----------------------------------------------------------------------------

def run(
    *,
    chunks_path: Path = INPUT_CHUNKS,
    graph_path: Path = INPUT_GRAPH,
    out_dir: Path = OUT_DIR,
    model_name: str = DEFAULT_MODEL,
    device: Optional[str] = DEFAULT_DEVICE,
) -> dict[str, Any]:
    print("[6-1] avvio costruzione indice")
    print(f"       chunks={chunks_path}")
    print(f"       graph={graph_path}")
    print(f"       modello={model_name}  device={device or 'auto'}")

    t0 = time.perf_counter()
    records, texts = _load_chunks(chunks_path)
    chunk_ids = [r["chunk_id"] for r in records]
    texts_list = [texts[cid] for cid in chunk_ids]
    t_load = time.perf_counter() - t0

    if not chunk_ids:
        raise SystemExit("Nessun chunk trovato in chunks.json")

    t1 = time.perf_counter()
    vectors = embed_texts(texts_list, model_name, device)
    t_embed = time.perf_counter() - t1

    if vectors.shape[0] != len(chunk_ids):
        raise SystemExit(
            f"Embedding count {vectors.shape[0]} != chunk count {len(chunk_ids)}"
        )
    dim = int(vectors.shape[1])

    out_dir.mkdir(parents=True, exist_ok=True)
    created_at = _now()

    np.save(out_dir / VECTORS_FILE, vectors)

    meta = {
        "created_at": created_at,
        "model": model_name,
        "num_chunks": len(records),
        "records": records,
    }
    (out_dir / META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / TEXTS_FILE).write_text(
        json.dumps(texts, ensure_ascii=False), encoding="utf-8"
    )

    graph_prov = _graph_provenance(graph_path)
    manifest = {
        "stage": "stage_6-1_index",
        "stage_version": STAGE_VERSION,
        "timestamp": created_at,
        "embedding": {
            "model": model_name,
            "device": device or "auto",
            "dim": dim,
            "normalized": True,
            "metric": "cosine",
        },
        "chunks": {
            "path": _rel(chunks_path),
            "sha256": _sha256(chunks_path),
            "num_chunks": len(records),
        },
        "graph": graph_prov,
        "artifacts": {
            "vectors": VECTORS_FILE,
            "meta": META_FILE,
            "chunk_texts": TEXTS_FILE,
        },
    }
    (out_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    index_log = {
        "stage": "stage_6-1_index",
        "stage_version": STAGE_VERSION,
        "timestamp": created_at,
        "parameters": {
            "model": model_name,
            "device": device or "auto",
            "batch_size": 8,
            "normalize_embeddings": True,
        },
        "counts": {
            "chunks": len(records),
            "vectors": int(vectors.shape[0]),
            "dim": dim,
        },
        "timings_s": {
            "load": round(t_load, 3),
            "embed": round(t_embed, 3),
            "total": round(time.perf_counter() - t0, 3),
        },
        "inputs": {
            "chunks": _rel(chunks_path),
            "graph": _rel(graph_path),
        },
        "output_dir": _rel(out_dir),
    }
    (out_dir / LOG_FILE).write_text(
        json.dumps(index_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[6-1] indice scritto in {out_dir}/")
    print(f"       {len(records)} chunk · dim {dim} · embed {t_embed:.1f}s")
    print(f"       grafo di riferimento: {graph_prov['path']} "
          f"(run={graph_prov['source_run']}, nodi={graph_prov['nodes_total']})")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 6-1: costruzione indice RAG ibrido.")
    ap.add_argument("--chunks", type=Path, default=INPUT_CHUNKS, help="chunks.json (stadio 2)")
    ap.add_argument("--graph", type=Path, default=INPUT_GRAPH, help="enriched_graph.json (stadio 5)")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="cartella di output")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="path/nome modello BGE-M3")
    ap.add_argument("--device", default=DEFAULT_DEVICE, help="es. 'cuda', 'cpu' (default: cuda)")
    args = ap.parse_args()
    run(
        chunks_path=args.chunks,
        graph_path=args.graph,
        out_dir=args.out,
        model_name=args.model,
        device=args.device,
    )


if __name__ == "__main__":
    main()
