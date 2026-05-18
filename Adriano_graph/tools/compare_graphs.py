#!/usr/bin/env python3
"""
Confronta grafi estratti (umano vs modello) e unisce più file JSON in un unico
documento con `extractions`. Per i blocchi `provenance` si considerano solo
`confidence` e `evidence_span`.

Confronto: salva il report in ``Adriano_graph/data/output/compare_results.json`` (override con ``-o``).
Niente allineamento per id testuali: solo conteggi e distribuzione per tipo di nodo/arco.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

# `Adriano_graph/` (cartella che contiene tools/ e data/)
ADRIANO_GRAPH_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CHUNKS = ADRIANO_GRAPH_ROOT / "data" / "stage_2" / "chunks.json"
DEFAULT_HUMAN_DIR = ADRIANO_GRAPH_ROOT / "data" / "stage_3" / "test"
DEFAULT_MODEL = ADRIANO_GRAPH_ROOT / "data" / "stage_3" / "extracted_graph_test.json"
DEFAULT_COMPARE_OUTPUT = ADRIANO_GRAPH_ROOT / "data" / "output" / "compare_results.json"

SCHEMA_NODE_TYPES = (
    "Person",
    "Event",
    "Place",
    "Phase",
    "Theme",
    "Reflection",
    "Work",
)
SCHEMA_EDGE_TYPES = (
    "INVOLVES",
    "LOCATED_AT",
    "DURING",
    "CREATED",
    "RELATED_TO",
    "EMBODIES",
    "REFLECTS_ON",
    "ECHOES",
    "CONTRASTS_WITH",
    "TRANSFORMS_INTO",
    "CAUSED",
    "FOLLOWS",
)

_CHUNK_ID_RE = re.compile(r"(ch_\d+)")


def canonical_chunk_id(*, path: Path | None = None, chunk_id_str: str | None = None) -> str | None:
    """Ricava l'id canonico `ch_XXXX` dal nome file o dalla stringa chunk_id."""
    if path is not None:
        m = _CHUNK_ID_RE.search(path.stem)
        if m:
            return m.group(1)
    if chunk_id_str:
        m = _CHUNK_ID_RE.search(chunk_id_str)
        if m:
            return m.group(1)
    return None


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_chunks_index(chunks_path: Path) -> dict[str, str]:
    """chunk_id -> testo del chunk."""
    data = load_json(chunks_path)
    chunks = data.get("chunks") or []
    out: dict[str, str] = {}
    for c in chunks:
        cid = c.get("chunk_id")
        text = c.get("text")
        if isinstance(cid, str) and isinstance(text, str):
            out[cid] = text
    return out


def extractions_from_file(path: Path) -> list[dict[str, Any]]:
    """
    Se il file ha `extractions`, li restituisce; altrimenti tratta il root come
    un'unica estrazione (es. singolo ch_XXXX.json della cartella test).
    """
    data = load_json(path)
    if isinstance(data, dict) and "extractions" in data:
        ex = data["extractions"]
        return list(ex) if isinstance(ex, list) else []
    if isinstance(data, dict):
        return [data]
    return []


def index_extractions_by_canonical(
    extractions: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in extractions:
        cid = e.get("chunk_id")
        key = canonical_chunk_id(chunk_id_str=cid if isinstance(cid, str) else None)
        if key:
            out[key] = e
    return out


def slim_provenance(prov: Any) -> dict[str, Any]:
    """Mantiene solo confidence ed evidence_span."""
    if not isinstance(prov, dict):
        return {}
    slim: dict[str, Any] = {}
    if "confidence" in prov:
        slim["confidence"] = prov["confidence"]
    if "evidence_span" in prov:
        slim["evidence_span"] = prov["evidence_span"]
    return slim


def strip_extraction_provenance(extraction: dict[str, Any]) -> dict[str, Any]:
    """Deep copy dell'estrazione con provenance ridotto su nodi e archi."""
    e = copy.deepcopy(extraction)
    for node in e.get("nodes") or []:
        if isinstance(node, dict) and "provenance" in node:
            node["provenance"] = slim_provenance(node.get("provenance"))
    for edge in e.get("edges") or []:
        if isinstance(edge, dict) and "provenance" in edge:
            edge["provenance"] = slim_provenance(edge.get("provenance"))
    return e


def join_graphs(paths: Iterable[Path | str]) -> dict[str, Any]:
    """
    Unisce n file (ciascuno con `extractions` o un singolo grafo) in
    {"extractions": [...]} con provenance ridotta.
    """
    merged: list[dict[str, Any]] = []
    for p in paths:
        path = Path(p)
        for ex in extractions_from_file(path):
            merged.append(strip_extraction_provenance(ex))
    return {"extractions": merged}


def _edges_signature(edges: list[Any]) -> set[tuple[str, str, str]]:
    sig: set[tuple[str, str, str]] = set()
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        s, t, typ = e.get("source_id"), e.get("target_id"), e.get("type")
        if isinstance(s, str) and isinstance(t, str) and isinstance(typ, str):
            sig.add((s, t, typ))
    return sig


def _type_counts(nodes: list[Any]) -> Counter[str]:
    c: Counter[str] = Counter()
    for n in nodes or []:
        if isinstance(n, dict):
            t = n.get("type")
            if isinstance(t, str):
                c[t] += 1
    return c


def _edge_type_counts(edges: list[Any]) -> Counter[str]:
    c: Counter[str] = Counter()
    for e in edges or []:
        if isinstance(e, dict):
            t = e.get("type")
            if isinstance(t, str):
                c[t] += 1
    return c


def _full_schema_counts(counter: Counter[str], schema: tuple[str, ...]) -> dict[str, int]:
    """Conteggi per ogni valore dello schema, inclusi quelli a zero."""
    return {k: int(counter.get(k, 0)) for k in schema}


def _counts_not_in_schema(counter: Counter[str], schema: tuple[str, ...]) -> dict[str, int]:
    schema_set = frozenset(schema)
    return {k: int(counter[k]) for k in sorted(counter.keys()) if k not in schema_set}


def _schema_node_breakdown(
    nodes: list[Any],
    schema: tuple[str, ...],
    counter: Counter[str],
) -> dict[str, dict[str, Any]]:
    """Per ogni tipo nello schema: conteggio + id ordinati (solo ``id`` stringa)."""
    type_to_ids: dict[str, list[str]] = {t: [] for t in schema}
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        nid = n.get("id")
        if isinstance(t, str) and t in type_to_ids and isinstance(nid, str):
            type_to_ids[t].append(nid)
    for t in type_to_ids:
        type_to_ids[t].sort()
    return {
        t: {
            "count": int(counter.get(t, 0)),
            "ids": type_to_ids[t],
        }
        for t in schema
    }


def _not_in_schema_node_details(nodes: list[Any], schema: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    schema_set = frozenset(schema)
    buckets: dict[str, list[str]] = {}
    counter = Counter()
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        nid = n.get("id")
        if not isinstance(t, str) or t in schema_set:
            continue
        counter[t] += 1
        if isinstance(nid, str):
            buckets.setdefault(t, []).append(nid)
    for t in buckets:
        buckets[t].sort()
    return {
        k: {"count": int(counter[k]), "ids": buckets.get(k, [])}
        for k in sorted(counter.keys())
    }


def compare_extractions(
    human: dict[str, Any],
    model: dict[str, Any],
    *,
    chunk_text: str | None = None,
) -> dict[str, Any]:
    """
    Confronta due estrazioni (stesso chunk). Restituisce un dict di metriche.
    (Nessun allineamento per id testuali: solo conteggi e distribuzioni per tipo.)
    """
    h_raw = human.get("nodes") or []
    m_raw = model.get("nodes") or []
    h_node_list = h_raw if isinstance(h_raw, list) else []
    m_node_list = m_raw if isinstance(m_raw, list) else []

    h_edges_raw = human.get("edges") or []
    m_edges_raw = model.get("edges") or []
    h_edge_list = h_edges_raw if isinstance(h_edges_raw, list) else []
    m_edge_list = m_edges_raw if isinstance(m_edges_raw, list) else []

    h_node_counter = _type_counts(h_node_list)
    m_node_counter = _type_counts(m_node_list)
    h_edge_counter = _edge_type_counts(h_edge_list)
    m_edge_counter = _edge_type_counts(m_edge_list)

    h_sig = _edges_signature(h_edge_list)
    m_sig = _edges_signature(m_edge_list)

    return {
        "chunk_text": chunk_text,
        "counts": {
            "nodes_human": len(h_node_list),
            "nodes_model": len(m_node_list),
            "edges_human": len(h_sig),
            "edges_model": len(m_sig),
        },
        "node_types": {
            "human": _schema_node_breakdown(h_node_list, SCHEMA_NODE_TYPES, h_node_counter),
            "model": _schema_node_breakdown(m_node_list, SCHEMA_NODE_TYPES, m_node_counter),
            "not_in_schema": {
                "human": _not_in_schema_node_details(h_node_list, SCHEMA_NODE_TYPES),
                "model": _not_in_schema_node_details(m_node_list, SCHEMA_NODE_TYPES),
            },
        },
        "edge_types": {
            "human": _full_schema_counts(h_edge_counter, SCHEMA_EDGE_TYPES),
            "model": _full_schema_counts(m_edge_counter, SCHEMA_EDGE_TYPES),
            "not_in_schema": {
                "human": _counts_not_in_schema(h_edge_counter, SCHEMA_EDGE_TYPES),
                "model": _counts_not_in_schema(m_edge_counter, SCHEMA_EDGE_TYPES),
            },
        },
    }


def apply_shrink_output_path(path: Path) -> Path:
    """Inserisce ``_shrunk`` tra stem e suffisso (es. ``a.json`` → ``a_shrunk.json``)."""
    return path.with_name(f"{path.stem}_shrunk{path.suffix}")


def shrink_compare_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Riduce un blocco chunk del report (niente testo; tipi come ``[human, model]``)."""
    if "error" in entry:
        return {"error": entry["error"]}

    h_nodes = entry["node_types"]["human"]
    m_nodes = entry["node_types"]["model"]
    node_types: dict[str, list[int]] = {}
    for t in SCHEMA_NODE_TYPES:
        hc = h_nodes[t]["count"]
        mc = m_nodes[t]["count"]
        if hc or mc:
            node_types[t] = [hc, mc]

    h_edges = entry["edge_types"]["human"]
    m_edges = entry["edge_types"]["model"]
    edge_types: dict[str, list[int]] = {}
    for t in SCHEMA_EDGE_TYPES:
        pair = [h_edges[t], m_edges[t]]
        if pair[0] or pair[1]:
            edge_types[t] = pair

    out: dict[str, Any] = {
        "counts": entry["counts"],
        "node_types": node_types,
        "edge_types": edge_types,
    }

    return out


def shrink_compare_report(report: dict[str, Any]) -> dict[str, Any]:
    chunks = report.get("chunks") or {}
    return {
        "chunks": {cid: shrink_compare_entry(e) for cid, e in chunks.items()},
    }


def run_compare(
    chunks_path: Path,
    human_dir: Path,
    model_path: Path,
    *,
    output_json: Path,
    print_summary: bool = False,
    shrink: bool = False,
) -> None:
    chunk_texts = load_chunks_index(chunks_path)
    model_data = load_json(model_path)
    model_extractions: list[dict[str, Any]] = []
    if isinstance(model_data, dict) and isinstance(model_data.get("extractions"), list):
        model_extractions = model_data["extractions"]
    model_by_c = index_extractions_by_canonical(model_extractions)

    human_files = sorted(human_dir.glob("*.json"))
    if not human_files:
        raise SystemExit(f"Nessun file .json in {human_dir}")

    report: dict[str, Any] = {"chunks": {}}

    for hf in human_files:
        c_id = canonical_chunk_id(path=hf)
        if not c_id:
            print(f"[skip] impossibile ricavare ch_* da {hf.name}")
            continue

        human_list = extractions_from_file(hf)
        if not human_list:
            continue
        human_ex = human_list[0]
        if c_id not in chunk_texts:
            print(f"[warn] chunk_id {c_id} non trovato in {chunks_path}")

        text = chunk_texts.get(c_id)
        model_ex = model_by_c.get(c_id)
        if model_ex is None:
            print(f"[warn] nessuna estrazione modello per {c_id}")
            report["chunks"][c_id] = {"error": "missing_model_extraction"}
            continue

        report["chunks"][c_id] = compare_extractions(
            human_ex,
            model_ex,
            chunk_text=text,
        )

    out_path = apply_shrink_output_path(output_json) if shrink else output_json
    to_write = shrink_compare_report(report) if shrink else report

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if shrink:
        payload = json.dumps(to_write, ensure_ascii=False, separators=(",", ":"))
    else:
        payload = json.dumps(to_write, ensure_ascii=False, indent=2)
    out_path.write_text(payload, encoding="utf-8")
    print(f"Report scritto in: {out_path.resolve()}")

    if not print_summary:
        return

    for c_id, data in report["chunks"].items():
        print(f"\n=== {c_id} ===")
        if "error" in data:
            print(data["error"])
            continue
        ct = data.get("chunk_text")
        if ct:
            if len(ct) > 400:
                print("Chunk (anteprima terminale, testo integrale nel JSON):")
                print(ct[:400] + "…")
            else:
                print("Chunk:")
                print(ct)
            print()
        co = data["counts"]
        print(
            f"Nodi: umano={co['nodes_human']}  modello={co['nodes_model']}  "
            f"Archi (triple uniche): umano={co['edges_human']}  modello={co['edges_model']}"
        )

        print("Tipi nodo (schema, umano | modello):")
        nth = data["node_types"]["human"]
        ntm = data["node_types"]["model"]
        for t in SCHEMA_NODE_TYPES:
            hu = nth[t]
            mu = ntm[t]
            print(
                f"  {t}: count {hu['count']} | {mu['count']}\n"
                f"       ids u:{hu['ids']}\n"
                f"       ids m:{mu['ids']}"
            )
        ntu_h = data["node_types"]["not_in_schema"]["human"]
        ntu_m = data["node_types"]["not_in_schema"]["model"]
        if ntu_h or ntu_m:
            print(f"Tipi nodo fuori schema: umano={ntu_h}  modello={ntu_m}")

        print("Tipi arco (schema, umano | modello):")
        eth = data["edge_types"]["human"]
        etm = data["edge_types"]["model"]
        for t in SCHEMA_EDGE_TYPES:
            print(f"  {t}: {eth[t]} | {etm[t]}")
        etu = data["edge_types"]["not_in_schema"]
        if etu["human"] or etu["model"]:
            print(f"Tipi arco fuori schema: umano={etu['human']}  modello={etu['model']}")


def main() -> None:
    """
    Guida all'utilizzo
    ----------------

    Sintassi (cwd ``Adriano_graph/``; dalla root della repo:
    ``python Adriano_graph/tools/compare_graphs.py``)::

        python tools/compare_graphs.py <compare|join> [opzioni]

    **compare** — Per ogni ``*.json`` in ``--human-dir`` (saltati i file senza un
    ``ch_*`` ricavabile dal nome), confronta l'estrazione umana (primo blocco del
    file) con l'estrazione del modello che ha lo stesso ``chunk_id`` nel file ``--model`` (envelope con ``extractions``). I testi dei
    chunk servono solo per il campo ``chunk_text`` nel report (testo integrale).

    Il report (conteggi e distribuzioni per tipo di nodo/arco, senza allineamento
    per id) va in ``-o``/``--output``.

    Opzioni ``compare``:

        --chunks PATH
            ``chunks.json`` con i testi. Default: ``data/stage_2/chunks.json``
            (sotto ``Adriano_graph/``).
        --human-dir PATH
            Cartella con i JSON di riferimento umano (``*.json``). Default:
            ``data/stage_3/test``.
        --model PATH
            JSON cumulativo del modello (lista ``extractions``). Default:
            ``data/stage_3/extracted_graph_test.json``.
        -o, --output PATH
            File del report di confronto. Default:
            ``data/output/compare_results.json``.
        --print
            Oltre a scrivere il JSON, stampa su stdout un riepilogo leggibile
            per chunk.
        --shrink
            Scrive un JSON compatto (nessun ``chunk_text``; tipi come coppie
            ``[umano, modello]``; senza tipi a conteggio zero) in un file il cui
            nome è ``<stem>_shrunk<ext>`` rispetto a ``-o``.

    **join** — Unisce uno o più file JSON: ciascuno può avere ``extractions`` oppure
    essere un singolo grafo; l'output è ``{"extractions": [...]}`` con ``provenance``
    ridotto a ``confidence`` e ``evidence_span``.

    Opzioni ``join``:

        inputs …
            Uno o più percorsi (obbligatori).
        -o, --output PATH
            Se indicato, scrive il JSON nel file; altrimenti stampa su stdout.

    Esempi::

        python tools/compare_graphs.py compare
        python tools/compare_graphs.py compare --model data/stage_3/mio_run.json -o data/output/report.json --print
        python tools/compare_graphs.py join grafo_a.json grafo_b.json -o unico.json
    """
    ap = argparse.ArgumentParser(description="Confronto grafi estratti e merge JSON.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_cmp = sub.add_parser("compare", help="Confronta cartella test umana vs file modello")
    p_cmp.add_argument(
        "--chunks",
        type=Path,
        default=DEFAULT_CHUNKS,
        help=f"Default: {DEFAULT_CHUNKS}",
    )
    p_cmp.add_argument(
        "--human-dir",
        type=Path,
        default=DEFAULT_HUMAN_DIR,
        help=f"Default: {DEFAULT_HUMAN_DIR}",
    )
    p_cmp.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL,
        help=f"Default: {DEFAULT_MODEL}",
    )
    p_cmp.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_COMPARE_OUTPUT,
        help=f"File JSON del report (default: {DEFAULT_COMPARE_OUTPUT})",
    )
    p_cmp.add_argument(
        "--print",
        action="store_true",
        help="Stampa anche il riepilogo leggibile su stdout",
    )
    p_cmp.add_argument(
        "--shrink",
        action="store_true",
        help="Output compatto in <stem>_shrunk<ext> (no testo; tipi [h,m])",
    )

    p_join = sub.add_parser("join", help="Unisce file in un unico JSON con extractions")
    p_join.add_argument("inputs", nargs="+", type=Path, help="File JSON da unire")
    p_join.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Scrive su file invece di stdout",
    )

    args = ap.parse_args()

    if args.command == "compare":
        run_compare(
            args.chunks,
            args.human_dir,
            args.model,
            output_json=args.output,
            print_summary=args.print,
            shrink=args.shrink,
        )
    elif args.command == "join":
        doc = join_graphs(args.inputs)
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text)


if __name__ == "__main__":
    main()
