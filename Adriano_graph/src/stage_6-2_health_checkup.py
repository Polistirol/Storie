# src/stage_6-2_health_checkup.py
"""
================================================================================
STADIO 6 — FASE 2: HEALTH CHECKUP (checkpoint di fine index)
================================================================================

COS'È
  Strumento di **convalida** dell'indice RAG costruito dal 6-1: che gli artefatti
  esistano e siano coerenti, che l'indice sia allineato alle sorgenti (chunk +
  grafo arricchito), che il grafo sia ben coperto dai chunk indicizzati e che il
  retrieval risponda in modo sensato a un piccolo set di domande di prova.
  Deterministico (gli embedding lo sono modulo GPU). Zero LLM. Stesso contratto
  di `stage_4-5` / `stage_5-6` (ADR-022/028).

COME SI USA
  python src/stage_6-2_health_checkup.py
  python src/stage_6-2_health_checkup.py --no-smoke      # salta lo smoke retrieval
  python src/stage_6-2_health_checkup.py --device cpu
  → apri `data/stage_6/2_health_checkup/dashboard.html`

INPUT (default)
  data/stage_6/1_index/{vectors.npy,meta.json,chunk_texts.json,manifest.json}
  data/stage_2/chunks.json                          ← per consistenza chunk
  data/stage_5/5_transforms/enriched_graph.json     ← per copertura nodi

OUTPUT (cartella data/stage_6/2_health_checkup/)
  dashboard.html   ← INIZIA DA QUI
  checks.json      esito di ogni controllo (pass / warn / fail / info / skip)
  metrics.json     numeri grezzi (copertura, dimensioni, smoke)
  review_queue.json item che richiedono attenzione (chunk/nodi scoperti, smoke vuoti)
  health_log.json  versioni, parametri, verdetto finale

VERDETTO FINALE (health_log.verdict)
  pass               indice OK, pronto per l'inferenza
  pass_with_warnings OK con avvisi (es. indice da rigenerare, copertura parziale)
  fail               bloccare: artefatti mancanti o incoerenti

Vedi ADR-029, ADR-022, PIPELINE.md sezione "Stadio 6 — Index".
================================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.repo_env import default_embed_device, default_embed_model  # noqa: E402

_DATA = PROJECT_ROOT / "data"
INDEX_DIR = _DATA / "stage_6" / "1_index"
INPUT_CHUNKS = _DATA / "stage_2" / "chunks.json"
INPUT_GRAPH = _DATA / "stage_5" / "5_transforms" / "enriched_graph.json"
OUT_DIR = _DATA / "stage_6" / "2_health_checkup"

VECTORS_FILE = "vectors.npy"
META_FILE = "meta.json"
TEXTS_FILE = "chunk_texts.json"
MANIFEST_FILE = "manifest.json"

STAGE_VERSION = "0.1.0"
DEFAULT_MODEL = default_embed_model()
DEFAULT_DEVICE = default_embed_device()
SMOKE_TOP_K = 8
MAX_SAMPLES = 12
COVERAGE_WARN_PCT = 95.0

SMOKE_QUESTIONS = [
    "Come mai sei andato dal tuo medico Ermogene stamattina?",
    "Che rapporto avevi con il tuo cavallo Boristene?",
    "Come vivi il pensiero della morte che si avvicina?",
    "Cosa provi quando senti lo sbuffare di un cervo nei boschi?",
    "Parlami di Antinoo: cosa significa per te?",
]

CheckStatus = Literal["pass", "warn", "fail", "info", "skip"]
Verdict = Literal["pass", "pass_with_warnings", "fail"]


# -----------------------------------------------------------------------------
# Catalogo controlli
# -----------------------------------------------------------------------------

CHECK_CATALOG: dict[str, dict[str, Any]] = {
    "index_artifacts": {
        "title": "Artefatti indice presenti e coerenti",
        "category": "struttura",
        "blocks_stage": True,
        "why": "L'inferenza richiede vectors.npy + meta.json + chunk_texts.json + manifest.json, "
               "con conteggi allineati (righe matrice = record = testi).",
        "what_to_check": "I quattro file esistono e num_vectors == num_records == num_texts.",
        "solutions": ["Rilanciare src/stage_6-1_index.py."],
    },
    "chunks_consistency": {
        "title": "Indice allineato ai chunk sorgente",
        "category": "freschezza",
        "blocks_stage": False,
        "why": "Se chunks.json è cambiato dopo la build, l'indice è stantio: gli embedding non "
               "corrispondono più al testo che l'inferenza mostrerà.",
        "what_to_check": "manifest.chunks.sha256 == sha256(chunks.json corrente); stessi chunk_id.",
        "solutions": ["Rigenerare l'indice: src/stage_6-1_index.py."],
    },
    "graph_consistency": {
        "title": "Indice allineato al grafo arricchito",
        "category": "freschezza",
        "blocks_stage": False,
        "why": "Il manifest fissa il grafo (versione+hash) contro cui l'indice è stato costruito. "
               "L'inferenza legge il grafo da stage_5: se è cambiato, copertura e link possono divergere.",
        "what_to_check": "manifest.graph.sha256 == sha256(enriched_graph.json corrente).",
        "solutions": [
            "Se il grafo è stato rigenerato: rilanciare src/stage_6-1_index.py.",
            "Accettabile se la modifica al grafo non tocca i chunk (solo archi/nodi sintetici).",
        ],
    },
    "node_coverage": {
        "title": "Copertura nodi via chunk indicizzati",
        "category": "qualità",
        "blocks_stage": False,
        "why": "Un nodo è raggiungibile dal retrieval solo se almeno un suo chunk di provenienza è "
               "indicizzato. Nodi senza chunk indicizzati sono invisibili al RAG vettoriale.",
        "what_to_check": "Percentuale di nodi con ≥1 chunk di provenienza nell'indice. Le Era "
                         "(deterministiche, 3.5) e alcuni nodi sintetici possono restare scoperti: atteso.",
        "solutions": [
            "Verificare che i chunk referenziati dal grafo siano tutti in chunks.json.",
            "Nodi Era/sintetici scoperti sono attesi (provenienza-sentinella), non bloccano.",
        ],
    },
    "chunk_reference": {
        "title": "Chunk indicizzati senza nodi",
        "category": "qualità",
        "blocks_stage": False,
        "why": "Chunk indicizzati che nessun nodo del grafo cita: testo recuperabile ma senza ancora "
               "nel grafo. Informativo: il chunk resta utile come passaggio sorgente.",
        "what_to_check": "Numero di chunk indicizzati mai referenziati da una provenienza di nodo.",
        "solutions": ["Nessuna azione necessaria; utile per capire dove il grafo è più rado."],
    },
    "smoke_retrieval": {
        "title": "Smoke retrieval su domande di prova",
        "category": "retrieval",
        "blocks_stage": False,
        "why": "Verifica end-to-end del retrieval (senza LLM): ogni domanda deve recuperare ≥1 chunk "
               "e toccare ≥1 nodo del grafo. Una domanda a vuoto segnala indice o mapping rotto.",
        "what_to_check": "Per ogni domanda: n. chunk > 0, n. nodi > 0, score plausibile, latenza.",
        "solutions": [
            "Se a vuoto: controllare il modello embed e l'allineamento chunk↔grafo.",
            "Eseguibile in dettaglio da inference/: python ask.py \"...\" --verbose --no-llm.",
        ],
    },
}


# -----------------------------------------------------------------------------
# IO
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


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _graph_chunk_map(graph_path: Path) -> tuple[dict[str, set[str]], dict[str, str], int, int]:
    """node_id → chunk_ids di provenienza; node_id → type; n_nodes, n_edges."""
    raw = _load_json(graph_path)
    node_chunks: dict[str, set[str]] = {}
    node_type: dict[str, str] = {}
    for n in raw.get("nodes", []):
        cids = {p["chunk_id"] for p in n.get("provenances", []) if p.get("chunk_id")}
        node_chunks[n["id"]] = cids
        node_type[n["id"]] = n.get("type", "?")
    return node_chunks, node_type, len(raw.get("nodes", [])), len(raw.get("edges", []))


# -----------------------------------------------------------------------------
# Smoke retrieval (search vettoriale self-contained)
# -----------------------------------------------------------------------------

def _run_smoke(
    vectors: np.ndarray,
    chunk_ids: list[str],
    chunk_to_nodes: dict[str, set[str]],
    model_name: str,
    device: Optional[str],
    top_k: int,
) -> dict[str, Any] | None:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    try:
        model = SentenceTransformer(model_name, device=device)
    except Exception as exc:  # modello assente / device non valido
        return {"error": str(exc)}

    results: list[dict[str, Any]] = []
    for q in SMOKE_QUESTIONS:
        t0 = time.perf_counter()
        qv = np.asarray(
            model.encode(q, normalize_embeddings=True, convert_to_numpy=True),
            dtype=np.float32,
        ).reshape(-1)
        scores = vectors @ qv
        k = min(top_k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        elapsed = time.perf_counter() - t0

        cids = [chunk_ids[int(i)] for i in top]
        nodes: set[str] = set()
        for cid in cids:
            nodes |= chunk_to_nodes.get(cid, set())
        results.append({
            "question": q,
            "n_chunks": len(cids),
            "top_chunks": cids[:5],
            "top_score": round(float(scores[int(top[0])]), 4),
            "n_nodes": len(nodes),
            "latency_s": round(elapsed, 3),
        })
    return {"results": results}


# -----------------------------------------------------------------------------
# Metriche
# -----------------------------------------------------------------------------

def compute_metrics(
    *,
    vectors: np.ndarray,
    meta: dict,
    texts: dict[str, str],
    manifest: dict,
    chunks_path: Path,
    graph_path: Path,
    node_chunks: dict[str, set[str]],
    node_type: dict[str, str],
    n_graph_nodes: int,
    n_graph_edges: int,
    smoke: dict[str, Any] | None,
) -> dict[str, Any]:
    records = meta.get("records", [])
    index_chunk_ids = [r["chunk_id"] for r in records]
    index_chunk_set = set(index_chunk_ids)

    # consistenza chunk
    cur_chunks_sha = _sha256(chunks_path)
    man_chunks_sha = (manifest.get("chunks") or {}).get("sha256")
    source_chunk_ids = {c["chunk_id"] for c in _load_json(chunks_path)["chunks"]}
    missing_in_index = sorted(source_chunk_ids - index_chunk_set)
    extra_in_index = sorted(index_chunk_set - source_chunk_ids)

    # consistenza grafo
    cur_graph_sha = _sha256(graph_path)
    man_graph_sha = (manifest.get("graph") or {}).get("sha256")

    # copertura nodi
    covered, uncovered = [], []
    referenced_chunks: set[str] = set()
    for nid, cids in node_chunks.items():
        referenced_chunks |= cids
        if cids & index_chunk_set:
            covered.append(nid)
        else:
            uncovered.append(nid)
    uncovered_by_type = Counter(node_type.get(nid, "?") for nid in uncovered)
    coverage_pct = round(100 * len(covered) / n_graph_nodes, 2) if n_graph_nodes else 0.0

    # chunk indicizzati senza nodi
    dead_chunks = sorted(index_chunk_set - referenced_chunks)
    # chunk referenziati dal grafo ma non indicizzati (gap reale)
    referenced_missing = sorted(referenced_chunks - index_chunk_set)

    return {
        "index": {
            "num_vectors": int(vectors.shape[0]),
            "dim": int(vectors.shape[1]) if vectors.ndim == 2 else None,
            "num_records": len(records),
            "num_texts": len(texts),
            "model": meta.get("model"),
            "created_at": meta.get("created_at"),
        },
        "chunks_consistency": {
            "manifest_sha": man_chunks_sha,
            "current_sha": cur_chunks_sha,
            "match": man_chunks_sha == cur_chunks_sha,
            "source_chunks": len(source_chunk_ids),
            "missing_in_index": missing_in_index,
            "extra_in_index": extra_in_index,
        },
        "graph_consistency": {
            "manifest_sha": man_graph_sha,
            "current_sha": cur_graph_sha,
            "match": man_graph_sha == cur_graph_sha,
            "graph_path": _rel(graph_path),
            "nodes_total": n_graph_nodes,
            "edges_total": n_graph_edges,
        },
        "node_coverage": {
            "nodes_total": n_graph_nodes,
            "nodes_covered": len(covered),
            "nodes_uncovered": len(uncovered),
            "coverage_pct": coverage_pct,
            "uncovered_by_type": dict(uncovered_by_type),
            "referenced_chunks_missing_from_index": referenced_missing,
        },
        "chunk_reference": {
            "indexed_chunks": len(index_chunk_set),
            "referenced_by_nodes": len(referenced_chunks & index_chunk_set),
            "dead_chunks": len(dead_chunks),
        },
        "smoke": smoke,
        "_uncovered_ids": uncovered,
        "_dead_chunks": dead_chunks,
    }


# -----------------------------------------------------------------------------
# Controlli
# -----------------------------------------------------------------------------

def _check_result(
    check_id: str,
    status: CheckStatus,
    *,
    count: int = 0,
    value: Any = None,
    message: str = "",
    samples: list[Any] | None = None,
) -> dict[str, Any]:
    meta = CHECK_CATALOG[check_id]
    return {
        "check_id": check_id,
        "title": meta["title"],
        "category": meta["category"],
        "status": status,
        "blocks_stage": meta["blocks_stage"],
        "count": count,
        "value": value,
        "message": message,
        "why": meta["why"],
        "what_to_check": meta["what_to_check"],
        "solutions": meta["solutions"],
        "samples": samples or [],
    }


def run_checks(metrics: dict[str, Any], *, smoke_requested: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    idx = metrics["index"]
    cc = metrics["chunks_consistency"]
    gc = metrics["graph_consistency"]
    nc = metrics["node_coverage"]
    cr = metrics["chunk_reference"]

    # 1) index_artifacts
    counts_ok = idx["num_vectors"] == idx["num_records"] == idx["num_texts"] and idx["num_vectors"] > 0
    if counts_ok:
        results.append(_check_result(
            "index_artifacts", "pass",
            count=idx["num_vectors"],
            message=f"{idx['num_vectors']} vettori (dim {idx['dim']}), record e testi allineati.",
        ))
    else:
        results.append(_check_result(
            "index_artifacts", "fail",
            message=f"Conteggi disallineati: vettori={idx['num_vectors']} "
                    f"record={idx['num_records']} testi={idx['num_texts']}.",
        ))

    # 2) chunks_consistency
    if cc["missing_in_index"] or cc["extra_in_index"] or not cc["match"]:
        results.append(_check_result(
            "chunks_consistency", "warn",
            count=len(cc["missing_in_index"]) + len(cc["extra_in_index"]),
            message=(
                f"Indice non allineato a chunks.json (hash "
                f"{'uguale' if cc['match'] else 'diverso'}): "
                f"{len(cc['missing_in_index'])} chunk mancanti, "
                f"{len(cc['extra_in_index'])} chunk di troppo."
            ),
            samples=(cc["missing_in_index"] + cc["extra_in_index"])[:MAX_SAMPLES],
        ))
    else:
        results.append(_check_result(
            "chunks_consistency", "pass",
            count=cc["source_chunks"],
            message=f"Indice allineato ai {cc['source_chunks']} chunk sorgente (hash identico).",
        ))

    # 3) graph_consistency
    if gc["match"]:
        results.append(_check_result(
            "graph_consistency", "pass",
            message=f"Indice costruito sul grafo corrente ({gc['nodes_total']} nodi, hash identico).",
        ))
    else:
        results.append(_check_result(
            "graph_consistency", "warn",
            message="Il grafo arricchito è cambiato dopo la build dell'indice (hash diverso). "
                    "Coperture e link sono calcolati sul grafo corrente; valutare la rigenerazione.",
        ))

    # 4) node_coverage
    ref_missing = nc["referenced_chunks_missing_from_index"]
    if ref_missing:
        results.append(_check_result(
            "node_coverage", "warn",
            count=len(ref_missing),
            value=nc["coverage_pct"],
            message=f"{len(ref_missing)} chunk referenziati dal grafo NON sono nell'indice "
                    f"(gap reale). Copertura nodi {nc['coverage_pct']}%.",
            samples=ref_missing[:MAX_SAMPLES],
        ))
    elif nc["coverage_pct"] < COVERAGE_WARN_PCT:
        results.append(_check_result(
            "node_coverage", "info",
            count=nc["nodes_uncovered"],
            value=nc["coverage_pct"],
            message=f"Copertura nodi {nc['coverage_pct']}% "
                    f"({nc['nodes_covered']}/{nc['nodes_total']}). "
                    f"Scoperti per tipo: {nc['uncovered_by_type']} (Era/sintetici attesi).",
        ))
    else:
        results.append(_check_result(
            "node_coverage", "pass",
            count=nc["nodes_covered"],
            value=nc["coverage_pct"],
            message=f"Copertura nodi {nc['coverage_pct']}% "
                    f"({nc['nodes_covered']}/{nc['nodes_total']}).",
        ))

    # 5) chunk_reference
    results.append(_check_result(
        "chunk_reference", "info",
        count=cr["dead_chunks"],
        message=f"{cr['referenced_by_nodes']}/{cr['indexed_chunks']} chunk indicizzati "
                f"sono citati da almeno un nodo; {cr['dead_chunks']} senza nodi.",
    ))

    # 6) smoke_retrieval
    smoke = metrics["smoke"]
    if not smoke_requested:
        results.append(_check_result(
            "smoke_retrieval", "skip",
            message="Smoke retrieval saltato (--no-smoke).",
        ))
    elif smoke is None:
        results.append(_check_result(
            "smoke_retrieval", "skip",
            message="sentence-transformers non disponibile: smoke retrieval saltato.",
        ))
    elif "error" in smoke:
        results.append(_check_result(
            "smoke_retrieval", "skip",
            message=f"Modello embed non caricabile, smoke saltato: {smoke['error'][:160]}",
        ))
    else:
        rows = smoke["results"]
        empty = [r for r in rows if r["n_chunks"] == 0 or r["n_nodes"] == 0]
        avg_lat = round(sum(r["latency_s"] for r in rows) / len(rows), 3) if rows else 0.0
        if empty:
            results.append(_check_result(
                "smoke_retrieval", "warn",
                count=len(empty),
                message=f"{len(empty)}/{len(rows)} domande senza chunk o senza nodi.",
                samples=[r["question"] for r in empty],
            ))
        else:
            results.append(_check_result(
                "smoke_retrieval", "pass",
                count=len(rows),
                message=f"Tutte le {len(rows)} domande recuperano chunk+nodi "
                        f"(latenza media {avg_lat}s).",
                samples=[
                    f"{r['question'][:50]}… → {r['n_chunks']} chunk, {r['n_nodes']} nodi "
                    f"(top {r['top_score']})"
                    for r in rows
                ],
            ))

    return results


def build_review_queue(metrics: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for cid in metrics["node_coverage"]["referenced_chunks_missing_from_index"]:
        items.append({"priority": 1, "kind": "chunk", "id": cid,
                      "reason": "referenced_by_graph_not_indexed"})
    smoke = metrics["smoke"]
    if smoke and "results" in smoke:
        for r in smoke["results"]:
            if r["n_chunks"] == 0 or r["n_nodes"] == 0:
                items.append({"priority": 1, "kind": "smoke", "id": r["question"],
                              "reason": "empty_retrieval"})
    items.sort(key=lambda x: (x["priority"], str(x["id"])))
    return {"total": len(items), "items": items}


def compute_verdict(checks: list[dict[str, Any]]) -> dict[str, Any]:
    fails = [c for c in checks if c["status"] == "fail" and c["blocks_stage"]]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        verdict: Verdict = "fail"
        headline = "Indice NON convalidato — rigenerare/correggere prima dell'inferenza."
    elif warns:
        verdict = "pass_with_warnings"
        headline = "Indice convalidato con avvisi — leggere i warn, l'inferenza può procedere."
    else:
        verdict = "pass"
        headline = "Indice convalidato — pronto per l'inferenza (cartella inference/)."
    return {
        "verdict": verdict,
        "headline": headline,
        "fail_count": len(fails),
        "warn_count": len(warns),
        "blocking_checks": [c["check_id"] for c in fails],
        "warning_checks": [c["check_id"] for c in warns],
    }


# -----------------------------------------------------------------------------
# Dashboard HTML
# -----------------------------------------------------------------------------

def _status_badge(status: str) -> str:
    colors = {
        "pass": ("#0d7a4a", "#e6f4ed"),
        "warn": ("#9a6700", "#fff8e6"),
        "fail": ("#b42318", "#fdecea"),
        "info": ("#175cd3", "#eff4ff"),
        "skip": ("#667085", "#f2f4f7"),
    }
    fg, bg = colors.get(status, ("#333", "#eee"))
    return (
        f'<span class="badge" style="color:{fg};background:{bg};border:1px solid {fg}22">'
        f"{status.upper()}</span>"
    )


def _verdict_banner(verdict: dict[str, Any]) -> str:
    colors = {
        "pass": ("#0d7a4a", "#e6f4ed"),
        "pass_with_warnings": ("#9a6700", "#fff8e6"),
        "fail": ("#b42318", "#fdecea"),
    }
    fg, bg = colors.get(verdict["verdict"], ("#333", "#eee"))
    label = verdict["verdict"].replace("_", " ").upper()
    return f"""
    <div class="verdict" style="background:{bg};border-left:4px solid {fg}">
      <div class="verdict-label" style="color:{fg}">{label}</div>
      <div class="verdict-headline">{html.escape(verdict['headline'])}</div>
      <div class="verdict-meta">fail blocanti: {verdict['fail_count']} · warn: {verdict['warn_count']}</div>
    </div>"""


def render_dashboard(
    *,
    metrics: dict[str, Any],
    checks: list[dict[str, Any]],
    verdict: dict[str, Any],
    health_log: dict[str, Any],
    review_queue: dict[str, Any],
) -> str:
    idx = metrics["index"]
    nc = metrics["node_coverage"]
    cards = [
        ("Chunk indicizzati", idx["num_vectors"]),
        ("Dim embedding", idx["dim"]),
        ("Nodi grafo", nc["nodes_total"]),
        ("Copertura nodi", f"{nc['coverage_pct']}%"),
        ("Nodi scoperti", nc["nodes_uncovered"]),
        ("Review queue", review_queue["total"]),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="card-n">{html.escape(str(v))}</div>'
        f'<div class="card-l">{html.escape(k)}</div></div>'
        for k, v in cards
    )

    check_rows = []
    for c in checks:
        samples_html = ""
        if c["samples"]:
            lines = "".join(f"<li><code>{html.escape(str(s))}</code></li>" for s in c["samples"][:8])
            samples_html = f"<ul class='samples'>{lines}</ul>"
        sol_html = "<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in c["solutions"]) + "</ul>"
        check_rows.append(f"""
        <tr class="check-row status-{c['status']}">
          <td>{_status_badge(c['status'])}</td>
          <td><strong>{html.escape(c['title'])}</strong><br>
              <span class="muted">{html.escape(c['check_id'])} · {html.escape(c['category'])}</span>
              {"<br><span class='block'>BLOCCA STADIO</span>" if c['blocks_stage'] else ""}</td>
          <td>{html.escape(c['message'])}
              {f"<br><span class='muted'>valore: {html.escape(str(c['value']))}</span>" if c.get('value') is not None else ""}</td>
          <td class="why">{html.escape(c['why'])}</td>
          <td class="actions"><p><strong>Cosa verificare</strong></p><p>{html.escape(c['what_to_check'])}</p>
              <p><strong>Soluzioni</strong></p>{sol_html}{samples_html}</td>
        </tr>""")

    guide_items = "".join(
        f"<li><strong>{html.escape(v['title'])}</strong> ({k}): {html.escape(v['why'][:120])}…</li>"
        for k, v in CHECK_CATALOG.items()
    )

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>Health checkup — Stadio 6 Index</title>
  <style>
    :root {{ font-family: "Segoe UI", system-ui, sans-serif; color: #1a1a1a; line-height: 1.45; }}
    body {{ max-width: 1200px; margin: 0 auto; padding: 24px; background: #fafafa; }}
    h1 {{ margin: 0 0 8px; font-size: 1.6rem; }}
    h2 {{ margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
    .muted {{ color: #667085; font-size: 0.9rem; }}
    .verdict {{ padding: 16px 20px; border-radius: 8px; margin: 20px 0; }}
    .verdict-label {{ font-weight: 700; font-size: 1.1rem; }}
    .verdict-headline {{ font-size: 1.05rem; margin-top: 4px; }}
    .verdict-meta {{ font-size: 0.85rem; color: #667085; margin-top: 8px; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }}
    .card {{ background: #fff; border: 1px solid #e4e7ec; border-radius: 8px; padding: 12px 16px; min-width: 100px; }}
    .card-n {{ font-size: 1.4rem; font-weight: 700; }}
    .card-l {{ font-size: 0.8rem; color: #667085; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 0.92rem; }}
    th, td {{ border: 1px solid #e4e7ec; padding: 10px 12px; vertical-align: top; }}
    th {{ background: #f2f4f7; text-align: left; }}
    .check-row.status-fail {{ background: #fffafa; }}
    .check-row.status-warn {{ background: #fffdf5; }}
    .why {{ max-width: 240px; font-size: 0.88rem; }}
    .actions {{ max-width: 320px; font-size: 0.88rem; }}
    .actions ul {{ margin: 4px 0; padding-left: 18px; }}
    .samples {{ margin-top: 8px; font-size: 0.82rem; }}
    .block {{ color: #b42318; font-size: 0.75rem; font-weight: 700; }}
    code {{ background: #f2f4f7; padding: 1px 4px; border-radius: 3px; font-size: 0.85em; }}
    .guide {{ background: #fff; border: 1px solid #e4e7ec; border-radius: 8px; padding: 16px; }}
    .meta {{ font-size: 0.85rem; color: #667085; }}
  </style>
</head>
<body>
  <h1>Health checkup — Stadio 6 Index</h1>
  <p class="meta">stage_6-2_health_checkup v{STAGE_VERSION} · {html.escape(health_log.get('timestamp', ''))} ·
     indice: <code>{html.escape(health_log.get('index_dir', ''))}</code></p>

  {_verdict_banner(verdict)}

  <h2>Metriche rapide</h2>
  <div class="cards">{cards_html}</div>

  <h2>Guida ai controlli</h2>
  <div class="guide">
    <p>Questo checkpoint convalida l'<strong>indice RAG ibrido</strong> (vettoriale sui chunk +
       collegamento al grafo arricchito) prodotto dal 6-1, prima di passare il testimone all'inferenza.</p>
    <ul>{guide_items}</ul>
    <p class="muted">Dettaglio completo nel docstring di <code>src/stage_6-2_health_checkup.py</code> e in <code>checks.json</code>.</p>
  </div>

  <h2>Esito controlli</h2>
  <table>
    <thead><tr><th>Status</th><th>Controllo</th><th>Messaggio</th><th>Perché conta</th><th>Verifica / Soluzioni</th></tr></thead>
    <tbody>{"".join(check_rows)}</tbody>
  </table>

  <h2>Review queue ({review_queue['total']} item)</h2>
  <p class="muted">Chunk referenziati ma non indicizzati + domande smoke a vuoto. File: <code>review_queue.json</code></p>

  <h2>Artefatti</h2>
  <ul>
    <li><code>dashboard.html</code> — questo report</li>
    <li><code>checks.json</code> — esiti strutturati</li>
    <li><code>metrics.json</code> — numeri grezzi</li>
    <li><code>review_queue.json</code> — coda attenzione</li>
    <li><code>health_log.json</code> — verdetto e metadata</li>
  </ul>
</body>
</html>"""


# -----------------------------------------------------------------------------
# Orchestrazione
# -----------------------------------------------------------------------------

def run(
    *,
    index_dir: Path = INDEX_DIR,
    chunks_path: Path = INPUT_CHUNKS,
    graph_path: Path = INPUT_GRAPH,
    out_dir: Path = OUT_DIR,
    model_name: str = DEFAULT_MODEL,
    device: Optional[str] = DEFAULT_DEVICE,
    run_smoke: bool = True,
) -> dict[str, Any]:
    vec_path = index_dir / VECTORS_FILE
    meta_path = index_dir / META_FILE
    texts_path = index_dir / TEXTS_FILE
    manifest_path = index_dir / MANIFEST_FILE

    missing = [p.name for p in (vec_path, meta_path, texts_path, manifest_path) if not p.is_file()]
    if missing:
        # Verdetto fail immediato: senza artefatti non si valuta nulla.
        out_dir.mkdir(parents=True, exist_ok=True)
        checks = [_check_result(
            "index_artifacts", "fail",
            message=f"Artefatti mancanti in {_rel(index_dir)}: {', '.join(missing)}. "
                    f"Esegui src/stage_6-1_index.py.",
        )]
        verdict = compute_verdict(checks)
        health_log = {
            "stage": "stage_6-2_health_checkup",
            "stage_version": STAGE_VERSION,
            "timestamp": _now(),
            "index_dir": _rel(index_dir),
            "verdict": verdict,
        }
        (out_dir / "checks.json").write_text(
            json.dumps({"checks": checks, "verdict": verdict}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "health_log.json").write_text(
            json.dumps(health_log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[6-2] FAIL — artefatti mancanti: {missing}")
        return health_log

    vectors = np.load(vec_path)
    meta = _load_json(meta_path)
    texts = _load_json(texts_path)
    manifest = _load_json(manifest_path)

    node_chunks, node_type, n_nodes, n_edges = _graph_chunk_map(graph_path)
    chunk_to_nodes: dict[str, set[str]] = {}
    for nid, cids in node_chunks.items():
        for cid in cids:
            chunk_to_nodes.setdefault(cid, set()).add(nid)

    smoke = None
    if run_smoke:
        index_chunk_ids = [r["chunk_id"] for r in meta.get("records", [])]
        smoke = _run_smoke(
            vectors, index_chunk_ids, chunk_to_nodes, model_name, device, SMOKE_TOP_K
        )

    metrics = compute_metrics(
        vectors=vectors,
        meta=meta,
        texts=texts,
        manifest=manifest,
        chunks_path=chunks_path,
        graph_path=graph_path,
        node_chunks=node_chunks,
        node_type=node_type,
        n_graph_nodes=n_nodes,
        n_graph_edges=n_edges,
        smoke=smoke,
    )
    public_metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}

    checks = run_checks(metrics, smoke_requested=run_smoke)
    verdict = compute_verdict(checks)
    review_queue = build_review_queue(metrics)

    health_log = {
        "stage": "stage_6-2_health_checkup",
        "stage_version": STAGE_VERSION,
        "timestamp": _now(),
        "index_dir": _rel(index_dir),
        "input_chunks": _rel(chunks_path),
        "input_graph": _rel(graph_path),
        "manifest": {
            "built_at": manifest.get("timestamp"),
            "embed_model": (manifest.get("embedding") or {}).get("model"),
            "graph_source_run": (manifest.get("graph") or {}).get("source_run"),
        },
        "parameters": {
            "smoke_top_k": SMOKE_TOP_K,
            "coverage_warn_pct": COVERAGE_WARN_PCT,
            "run_smoke": run_smoke,
        },
        "verdict": verdict,
        "summary": {
            "indexed_chunks": public_metrics["index"]["num_vectors"],
            "coverage_pct": public_metrics["node_coverage"]["coverage_pct"],
            "checks_total": len(checks),
            "review_queue_total": review_queue["total"],
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(public_metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "checks.json").write_text(
        json.dumps({"checks": checks, "verdict": verdict}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "review_queue.json").write_text(
        json.dumps(review_queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "health_log.json").write_text(
        json.dumps(health_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "dashboard.html").write_text(
        render_dashboard(
            metrics=public_metrics,
            checks=checks,
            verdict=verdict,
            health_log=health_log,
            review_queue=review_queue,
        ),
        encoding="utf-8",
    )

    print(f"[6-2] input indice: {index_dir}")
    print(f"       output: {out_dir}/")
    print(f"       verdetto: {verdict['verdict'].upper()} — {verdict['headline']}")
    print(f"       copertura nodi: {public_metrics['node_coverage']['coverage_pct']}% · "
          f"review queue: {review_queue['total']}")
    return health_log


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 6-2: health checkup dell'indice RAG.")
    ap.add_argument("--index-dir", type=Path, default=INDEX_DIR, help="cartella indice (output 6-1)")
    ap.add_argument("--chunks", type=Path, default=INPUT_CHUNKS, help="chunks.json (stadio 2)")
    ap.add_argument("--graph", type=Path, default=INPUT_GRAPH, help="enriched_graph.json (stadio 5)")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="cartella di output")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="path/nome modello BGE-M3 (smoke)")
    ap.add_argument("--device", default=DEFAULT_DEVICE, help="es. 'cuda', 'cpu' (default: cuda)")
    ap.add_argument("--no-smoke", action="store_true", help="salta lo smoke retrieval (no modello)")
    args = ap.parse_args()
    run(
        index_dir=args.index_dir,
        chunks_path=args.chunks,
        graph_path=args.graph,
        out_dir=args.out,
        model_name=args.model,
        device=args.device,
        run_smoke=not args.no_smoke,
    )


if __name__ == "__main__":
    main()
