#!/usr/bin/env python3
"""
Analisi del knowledge graph estratto in stadio 3.

Legge `extracted_graph.json` (`{"extractions": [...]}`) e, per i metadati
di run, il sibling `extraction_log.json` nella stessa cartella (override con
``--log``). Costruisce un grafo globale deduplicato (nodi per `id`, archi per
`(source_id, target_id, type)`) e produce un report HTML self-contained con
figure Plotly + un sidecar `metrics.json` con i numeri grezzi.

Risponde alle domande della checklist in PIPELINE / vault:
- conteggi e distribuzioni di nodi e archi
- hub e centralità (top N globale, top N per tipo, anomalie a grado 1)
- isolamento e connettività (isolati, orfani rispetto ad Adriano, componenti)
- qualità degli Event (mancanze di LOCATED_AT / DURING / INVOLVES / EMBODIES,
  Event Adriano-only, Event con grado > soglia)
- Phase come spina dorsale (DURING entranti, isolate, catene TRANSFORMS_INTO,
  Event top-degree senza DURING)
- Reflection (distribuzione per chunk, REFLECTS_ON orfani, tipo dei target)
- Theme (EMBODIES entranti, grado 1, top 10)
- Archi narrativi (ECHOES con reciprocità, CAUSED, FOLLOWS, CONTRASTS_WITH,
  TRANSFORMS_INTO, RELATED_TO)

Vista non orientata per i conteggi di grado; per le metriche intrinsecamente
orientate (LOCATED_AT, DURING, REFLECTS_ON target, TRANSFORMS_INTO chains) si
lavora direttamente sulla tabella degli archi.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import networkx as nx
import plotly.graph_objects as go

# ----------------------------------------------------------------------------
# Costanti / schema (allineate a src/schema.py e tools/compare_graphs.py)
# ----------------------------------------------------------------------------

ADRIANO_GRAPH_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_INPUT = (
    ADRIANO_GRAPH_ROOT
    / "data"
    / "stage_3"
    / "full_runs"
    / "18-05-2026_16-51"
    / "extracted_graph.json"
)
DEFAULT_OUTPUT_DIR = ADRIANO_GRAPH_ROOT / "data" / "output" / "extraction_analysis"

SCHEMA_NODE_TYPES: tuple[str, ...] = (
    "Person",
    "Event",
    "Place",
    "Phase",
    "Theme",
    "Reflection",
    "Work",
)
SCHEMA_EDGE_TYPES: tuple[str, ...] = (
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

NARRATIVE_EDGE_TYPES: tuple[str, ...] = (
    "ECHOES",
    "CAUSED",
    "FOLLOWS",
    "CONTRASTS_WITH",
    "TRANSFORMS_INTO",
    "RELATED_TO",
)

DEFAULT_ADRIANO_ID = "adriano"
DEFAULT_EVENT_HIGH_DEGREE = 10
DEFAULT_TOP_N = 30
DEFAULT_TOP_PER_TYPE = 10

# Metadati di run letti da extraction_log.json (o, in fallback, dall'header
# inline legacy di extracted_graph.json).
RUN_METADATA_KEYS: tuple[str, ...] = (
    "source",
    "created_at",
    "stage_version",
    "schema_version",
    "prompt_version",
    "model",
    "params",
    "mode",
    "started_at",
    "finished_at",
    "n_chunks_requested",
    "total_chunks_processed",
)


# ----------------------------------------------------------------------------
# IO
# ----------------------------------------------------------------------------


def default_log_path(graph_path: Path) -> Path:
    """Path di default del log: stessa cartella di `extracted_graph.json`."""
    return graph_path.parent / "extraction_log.json"


def load_run_metadata(log_path: Path) -> dict[str, Any]:
    """Carica i metadati di run da `extraction_log.json`."""
    if not log_path.exists():
        return {}

    with log_path.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise SystemExit(f"Formato inatteso in {log_path}: root non è un oggetto JSON")

    return {k: data[k] for k in RUN_METADATA_KEYS if k in data}


def load_extractions(
    graph_path: Path,
    log_path: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    """Carica estrazioni e metadati di run.

    - Grafo: `graph_path` deve contenere la lista `extractions`.
    - Metadati: preferibilmente da `log_path` (default: sibling
      `extraction_log.json`). Se il log manca, usa l'header inline legacy
      nel file grafo (compatibilità con run precedenti allo split output/log).
    """
    with graph_path.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise SystemExit(f"Formato inatteso in {graph_path}: root non è un oggetto JSON")

    extractions = data.get("extractions")
    if not isinstance(extractions, list):
        raise SystemExit(f"Formato inatteso in {graph_path}: manca lista `extractions`")

    inline_header = {k: v for k, v in data.items() if k != "extractions"}

    resolved_log_path = log_path or default_log_path(graph_path)
    log_header = load_run_metadata(resolved_log_path)

    if log_header:
        header = log_header
    else:
        header = inline_header

    return header, extractions, resolved_log_path


# ----------------------------------------------------------------------------
# Merge globale: nodes_global, edges_global, networkx.Graph
# ----------------------------------------------------------------------------


def build_global_graph(
    extractions: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str, str], dict[str, Any]],
    dict[str, int],
    nx.Graph,
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    """
    Merge per `id` (nodi) e per tripla `(source_id, target_id, type)` (archi).

    Ritorna:
    - nodes_global: id -> {type, name, description, aliases, chunks (set), first_chunk}
    - edges_global: (src, tgt, type) -> {description, chunks (set)}
    - reflections_per_chunk: chunk_id -> numero di Reflection nel chunk
    - G: nx.Graph non orientato, un edge per tripla, attributo edge_types (lista)
    - warnings: lista di stringhe di anomalie strutturali (es. tipo discordante)
    - node_occurrences: una entry per ogni occorrenza nodo per-chunk, con la
      provenance (confidence ed evidence_span inclusi). Le metriche di
      provenance lavorano qui, non sulla vista deduplicata.
    - edge_occurrences: simmetrico per gli archi.
    - empty_chunk_ids: chunk_id presenti in extractions con zero nodi E zero archi.
    """
    nodes_global: dict[str, dict[str, Any]] = {}
    edges_global: dict[tuple[str, str, str], dict[str, Any]] = {}
    reflections_per_chunk: dict[str, int] = {}
    warnings: list[str] = []
    node_occurrences: list[dict[str, Any]] = []
    edge_occurrences: list[dict[str, Any]] = []
    empty_chunk_ids: list[str] = []

    for extraction in extractions:
        if not isinstance(extraction, dict):
            continue
        chunk_id = extraction.get("chunk_id")
        if not isinstance(chunk_id, str):
            continue

        nodes = extraction.get("nodes") or []
        edges = extraction.get("edges") or []

        if not nodes and not edges:
            empty_chunk_ids.append(chunk_id)

        refl_count = 0
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = node.get("id")
            ntype = node.get("type")
            if not isinstance(nid, str) or not isinstance(ntype, str):
                continue
            if ntype == "Reflection":
                refl_count += 1

            prov = node.get("provenance") or {}
            confidence = prov.get("confidence") if isinstance(prov, dict) else None
            evidence_span = prov.get("evidence_span") if isinstance(prov, dict) else None
            node_occurrences.append(
                {
                    "id": nid,
                    "type": ntype,
                    "name": node.get("name") or nid,
                    "chunk_id": chunk_id,
                    "confidence": confidence,
                    "evidence_span": evidence_span,
                }
            )

            if nid not in nodes_global:
                nodes_global[nid] = {
                    "type": ntype,
                    "name": node.get("name") or nid,
                    "description": node.get("description"),
                    "aliases": set(node.get("aliases") or []),
                    "chunks": {chunk_id},
                    "first_chunk": chunk_id,
                }
            else:
                rec = nodes_global[nid]
                if rec["type"] != ntype:
                    warnings.append(
                        f"Tipo discordante per id={nid!r}: "
                        f"{rec['type']} (in {rec['first_chunk']}) vs {ntype} (in {chunk_id})"
                    )
                rec["chunks"].add(chunk_id)
                rec["aliases"].update(node.get("aliases") or [])

        reflections_per_chunk[chunk_id] = refl_count

        for edge in edges:
            if not isinstance(edge, dict):
                continue
            src = edge.get("source_id")
            tgt = edge.get("target_id")
            etype = edge.get("type")
            if not (isinstance(src, str) and isinstance(tgt, str) and isinstance(etype, str)):
                continue
            prov = edge.get("provenance") or {}
            confidence = prov.get("confidence") if isinstance(prov, dict) else None
            evidence_span = prov.get("evidence_span") if isinstance(prov, dict) else None
            edge_occurrences.append(
                {
                    "source_id": src,
                    "target_id": tgt,
                    "type": etype,
                    "chunk_id": chunk_id,
                    "confidence": confidence,
                    "evidence_span": evidence_span,
                }
            )
            key = (src, tgt, etype)
            if key not in edges_global:
                edges_global[key] = {
                    "description": edge.get("description"),
                    "chunks": {chunk_id},
                }
            else:
                edges_global[key]["chunks"].add(chunk_id)

    G: nx.Graph = nx.Graph()
    for nid, rec in nodes_global.items():
        G.add_node(nid, type=rec["type"], name=rec["name"])

    for (src, tgt, etype), rec in edges_global.items():
        if src not in nodes_global or tgt not in nodes_global:
            warnings.append(
                f"Arco {etype} {src!r}->{tgt!r} fa riferimento a un nodo non estratto"
            )
            for missing in (src, tgt):
                if missing not in nodes_global:
                    nodes_global[missing] = {
                        "type": "Unknown",
                        "name": missing,
                        "description": None,
                        "aliases": set(),
                        "chunks": set(),
                        "first_chunk": None,
                    }
                    G.add_node(missing, type="Unknown", name=missing)
        if G.has_edge(src, tgt):
            etypes = G.edges[src, tgt].setdefault("edge_types", set())
            etypes.add(etype)
        else:
            G.add_edge(src, tgt, edge_types={etype})

    return (
        nodes_global,
        edges_global,
        reflections_per_chunk,
        G,
        warnings,
        node_occurrences,
        edge_occurrences,
        empty_chunk_ids,
    )


# ----------------------------------------------------------------------------
# Helper: serializzazione e indicizzazione
# ----------------------------------------------------------------------------


def _node_label(nid: str, nodes_global: dict[str, dict[str, Any]]) -> str:
    rec = nodes_global.get(nid)
    if rec is None:
        return nid
    name = rec.get("name") or nid
    return f"{name} [{rec.get('type', '?')}]  ({nid})"


def _short_label(nid: str, nodes_global: dict[str, dict[str, Any]], max_len: int = 60) -> str:
    rec = nodes_global.get(nid)
    if rec is None:
        return nid
    name = rec.get("name") or nid
    ntype = rec.get("type", "?")
    label = f"{name} [{ntype}]"
    if len(label) > max_len:
        label = label[: max_len - 1] + "…"
    return label


def _outgoing_index(
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, list[tuple[str, str]]]:
    """source_id -> list of (target_id, edge_type)."""
    idx: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (src, tgt, etype) in edges_global:
        idx[src].append((tgt, etype))
    return idx


def _incoming_index(
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, list[tuple[str, str]]]:
    """target_id -> list of (source_id, edge_type)."""
    idx: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (src, tgt, etype) in edges_global:
        idx[tgt].append((src, etype))
    return idx


# ----------------------------------------------------------------------------
# Sezione 1: conteggi e distribuzioni
# ----------------------------------------------------------------------------


def compute_counts(
    nodes_global: dict[str, dict[str, Any]],
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    node_types_counter: Counter[str] = Counter(
        rec["type"] for rec in nodes_global.values()
    )
    edge_types_counter: Counter[str] = Counter(
        etype for (_, _, etype) in edges_global
    )

    by_node_type = {t: int(node_types_counter.get(t, 0)) for t in SCHEMA_NODE_TYPES}
    by_node_type_extra = {
        t: int(c)
        for t, c in node_types_counter.items()
        if t not in SCHEMA_NODE_TYPES
    }

    by_edge_type = {t: int(edge_types_counter.get(t, 0)) for t in SCHEMA_EDGE_TYPES}
    by_edge_type_extra = {
        t: int(c)
        for t, c in edge_types_counter.items()
        if t not in SCHEMA_EDGE_TYPES
    }

    return {
        "nodes_total": len(nodes_global),
        "edges_total": len(edges_global),
        "by_node_type": by_node_type,
        "by_node_type_not_in_schema": by_node_type_extra,
        "by_edge_type": by_edge_type,
        "by_edge_type_not_in_schema": by_edge_type_extra,
    }


def fig_node_types(counts: dict[str, Any]) -> go.Figure:
    types = list(SCHEMA_NODE_TYPES) + sorted(counts["by_node_type_not_in_schema"].keys())
    values = [counts["by_node_type"].get(t, 0) for t in SCHEMA_NODE_TYPES] + [
        counts["by_node_type_not_in_schema"][t]
        for t in sorted(counts["by_node_type_not_in_schema"].keys())
    ]
    fig = go.Figure(
        data=[
            go.Bar(
                x=types,
                y=values,
                text=values,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title=f"Nodi per tipo (totale: {counts['nodes_total']})",
        yaxis_title="conteggio",
        xaxis_title="tipo di nodo",
        bargap=0.25,
    )
    return fig


def fig_edge_types(counts: dict[str, Any]) -> go.Figure:
    types = list(SCHEMA_EDGE_TYPES) + sorted(counts["by_edge_type_not_in_schema"].keys())
    values = [counts["by_edge_type"].get(t, 0) for t in SCHEMA_EDGE_TYPES] + [
        counts["by_edge_type_not_in_schema"][t]
        for t in sorted(counts["by_edge_type_not_in_schema"].keys())
    ]
    fig = go.Figure(
        data=[
            go.Bar(
                x=types,
                y=values,
                text=values,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title=f"Archi per tipo (totale triple uniche: {counts['edges_total']})",
        yaxis_title="conteggio",
        xaxis_title="tipo di arco",
        bargap=0.25,
    )
    return fig


# ----------------------------------------------------------------------------
# Sezione 2: hub e centralità
# ----------------------------------------------------------------------------


def compute_hubs(
    G: nx.Graph,
    nodes_global: dict[str, dict[str, Any]],
    top_n: int = DEFAULT_TOP_N,
    top_per_type: int = DEFAULT_TOP_PER_TYPE,
) -> dict[str, Any]:
    degrees: list[tuple[str, int]] = sorted(
        ((nid, deg) for nid, deg in G.degree()),
        key=lambda x: (-x[1], x[0]),
    )
    by_id_degree = dict(degrees)

    top_overall = [
        {
            "id": nid,
            "name": nodes_global[nid]["name"],
            "type": nodes_global[nid]["type"],
            "degree": deg,
        }
        for nid, deg in degrees[:top_n]
    ]

    top_by_type: dict[str, list[dict[str, Any]]] = {}
    for ntype in ("Person", "Place", "Theme", "Phase"):
        filtered = [(nid, deg) for nid, deg in degrees if nodes_global[nid]["type"] == ntype]
        top_by_type[ntype] = [
            {
                "id": nid,
                "name": nodes_global[nid]["name"],
                "degree": deg,
            }
            for nid, deg in filtered[:top_per_type]
        ]

    degree_one_person = [
        {"id": nid, "name": nodes_global[nid]["name"], "degree": deg}
        for nid, deg in degrees
        if nodes_global[nid]["type"] == "Person" and deg == 1
    ]
    degree_one_place = [
        {"id": nid, "name": nodes_global[nid]["name"], "degree": deg}
        for nid, deg in degrees
        if nodes_global[nid]["type"] == "Place" and deg == 1
    ]

    return {
        "top_overall": top_overall,
        "top_by_type": top_by_type,
        "degree_one_person": degree_one_person,
        "degree_one_place": degree_one_place,
        "by_id_degree": by_id_degree,
    }


def fig_top_overall(top_overall: list[dict[str, Any]]) -> go.Figure:
    labels = [
        f"{r['name']} [{r['type']}]"
        for r in top_overall
    ]
    degs = [r["degree"] for r in top_overall]
    fig = go.Figure(
        data=[
            go.Bar(
                x=degs,
                y=labels,
                orientation="h",
                text=degs,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title=f"Top {len(top_overall)} nodi per grado (non orientato)",
        xaxis_title="grado",
        height=max(450, 22 * len(top_overall) + 200),
    )
    fig.update_yaxes(autorange="reversed")
    return fig


def fig_top_by_type(top_by_type: dict[str, list[dict[str, Any]]]) -> go.Figure:
    from plotly.subplots import make_subplots

    types = ("Person", "Place", "Theme", "Phase")
    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=[f"Top {DEFAULT_TOP_PER_TYPE} {t}" for t in types],
        horizontal_spacing=0.18,
        vertical_spacing=0.18,
    )
    positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
    for (r, c), t in zip(positions, types):
        items = top_by_type.get(t, [])
        labels = [it["name"] for it in items]
        degs = [it["degree"] for it in items]
        fig.add_trace(
            go.Bar(
                x=degs,
                y=labels,
                orientation="h",
                text=degs,
                textposition="outside",
                name=t,
                showlegend=False,
            ),
            row=r,
            col=c,
        )
        fig.update_yaxes(autorange="reversed", row=r, col=c)
    fig.update_layout(
        title="Top per tipo (Person / Place / Theme / Phase)",
        height=700,
    )
    return fig


# ----------------------------------------------------------------------------
# Sezione 3: isolamento e connettività
# ----------------------------------------------------------------------------


def compute_isolation(
    G: nx.Graph,
    nodes_global: dict[str, dict[str, Any]],
    adriano_id: str,
) -> dict[str, Any]:
    isolated = [
        {"id": nid, "name": nodes_global[nid]["name"], "type": nodes_global[nid]["type"]}
        for nid in G.nodes
        if G.degree(nid) == 0
    ]
    isolated.sort(key=lambda r: (r["type"], r["id"]))

    orphans_to_adriano: list[dict[str, Any]] = []
    if adriano_id in G:
        for neigh in G.neighbors(adriano_id):
            if G.degree(neigh) == 1:
                orphans_to_adriano.append(
                    {
                        "id": neigh,
                        "name": nodes_global[neigh]["name"],
                        "type": nodes_global[neigh]["type"],
                    }
                )
        orphans_to_adriano.sort(key=lambda r: (r["type"], r["id"]))

    components = sorted(
        (list(c) for c in nx.connected_components(G)),
        key=lambda c: -len(c),
    )
    component_sizes = [len(c) for c in components]
    giant_size = component_sizes[0] if component_sizes else 0
    giant_fraction = giant_size / G.number_of_nodes() if G.number_of_nodes() else 0.0

    small_components_preview = []
    for comp in components[1:]:
        small_components_preview.append(
            {
                "size": len(comp),
                "members": [
                    {
                        "id": nid,
                        "name": nodes_global[nid]["name"],
                        "type": nodes_global[nid]["type"],
                    }
                    for nid in sorted(comp)
                ],
            }
        )

    return {
        "isolated_total": len(isolated),
        "isolated": isolated,
        "orphans_to_adriano_total": len(orphans_to_adriano),
        "orphans_to_adriano": orphans_to_adriano,
        "components_count": len(components),
        "component_sizes": component_sizes,
        "giant_component_size": giant_size,
        "giant_component_fraction": giant_fraction,
        "small_components": small_components_preview,
    }


def fig_component_sizes(component_sizes: list[int]) -> go.Figure:
    if not component_sizes:
        return go.Figure()
    fig = go.Figure(data=[go.Histogram(x=component_sizes, nbinsx=min(40, max(5, len(component_sizes))))])
    fig.update_layout(
        title=(
            f"Dimensioni delle componenti connesse "
            f"(numero componenti: {len(component_sizes)}, "
            f"componente gigante: {component_sizes[0]} nodi)"
        ),
        xaxis_title="dimensione (numero di nodi)",
        yaxis_title="numero di componenti",
        bargap=0.05,
    )
    return fig


# ----------------------------------------------------------------------------
# Sezione 4: qualità degli Event
# ----------------------------------------------------------------------------


def compute_event_quality(
    nodes_global: dict[str, dict[str, Any]],
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
    G: nx.Graph,
    adriano_id: str,
    high_degree_threshold: int,
) -> dict[str, Any]:
    out_idx = _outgoing_index(edges_global)
    in_idx = _incoming_index(edges_global)

    event_ids = [nid for nid, rec in nodes_global.items() if rec["type"] == "Event"]

    missing_place: list[dict[str, Any]] = []
    missing_phase: list[dict[str, Any]] = []
    missing_other_person: list[dict[str, Any]] = []
    missing_theme: list[dict[str, Any]] = []
    only_adriano: list[dict[str, Any]] = []
    high_degree: list[dict[str, Any]] = []

    for eid in event_ids:
        outs = out_idx.get(eid, [])
        ins = in_idx.get(eid, [])

        has_located = any(
            etype == "LOCATED_AT" and nodes_global.get(tgt, {}).get("type") == "Place"
            for tgt, etype in outs
        )
        has_during = any(
            etype == "DURING" and nodes_global.get(tgt, {}).get("type") == "Phase"
            for tgt, etype in outs
        )

        persons_involved = {
            tgt
            for tgt, etype in outs
            if etype == "INVOLVES" and nodes_global.get(tgt, {}).get("type") == "Person"
        }
        has_other_person = bool(persons_involved - {adriano_id})

        has_embodies_theme = any(
            etype == "EMBODIES" and nodes_global.get(tgt, {}).get("type") == "Theme"
            for tgt, etype in outs
        )

        is_only_adriano = persons_involved == {adriano_id}

        info = {
            "id": eid,
            "name": nodes_global[eid]["name"],
            "degree": int(G.degree(eid)) if eid in G else 0,
            "chunks": sorted(nodes_global[eid]["chunks"]),
        }

        if not has_located:
            missing_place.append(info)
        if not has_during:
            missing_phase.append(info)
        if not has_other_person:
            missing_other_person.append(info)
        if not has_embodies_theme:
            missing_theme.append(info)
        if is_only_adriano:
            only_adriano.append(info)
        if info["degree"] > high_degree_threshold:
            high_degree.append(info)

    for lst in (
        missing_place,
        missing_phase,
        missing_other_person,
        missing_theme,
        only_adriano,
        high_degree,
    ):
        lst.sort(key=lambda r: (-r["degree"], r["id"]))

    return {
        "event_total": len(event_ids),
        "missing_place_count": len(missing_place),
        "missing_phase_count": len(missing_phase),
        "missing_other_person_count": len(missing_other_person),
        "missing_theme_count": len(missing_theme),
        "only_adriano_count": len(only_adriano),
        "high_degree_count": len(high_degree),
        "high_degree_threshold": high_degree_threshold,
        "missing_place": missing_place,
        "missing_phase": missing_phase,
        "missing_other_person": missing_other_person,
        "missing_theme": missing_theme,
        "only_adriano": only_adriano,
        "high_degree": high_degree,
    }


def fig_event_quality_summary(eq: dict[str, Any]) -> go.Figure:
    categories = [
        "senza LOCATED_AT",
        "senza DURING",
        "senza altra Person",
        "senza Theme",
        "solo Adriano",
        f"grado > {eq['high_degree_threshold']}",
    ]
    counts = [
        eq["missing_place_count"],
        eq["missing_phase_count"],
        eq["missing_other_person_count"],
        eq["missing_theme_count"],
        eq["only_adriano_count"],
        eq["high_degree_count"],
    ]
    fig = go.Figure(
        data=[
            go.Bar(
                x=categories,
                y=counts,
                text=counts,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title=f"Qualità degli Event (totale Event: {eq['event_total']})",
        yaxis_title="numero di Event",
        bargap=0.25,
    )
    return fig


# ----------------------------------------------------------------------------
# Sezione 5: Phase come spina dorsale
# ----------------------------------------------------------------------------


def compute_phases(
    nodes_global: dict[str, dict[str, Any]],
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
    G: nx.Graph,
    hubs: dict[str, Any],
) -> dict[str, Any]:
    in_idx = _incoming_index(edges_global)
    phase_ids = [nid for nid, rec in nodes_global.items() if rec["type"] == "Phase"]

    phase_event_count: list[dict[str, Any]] = []
    for pid in phase_ids:
        n_events = sum(
            1
            for src, etype in in_idx.get(pid, [])
            if etype == "DURING" and nodes_global.get(src, {}).get("type") == "Event"
        )
        phase_event_count.append(
            {
                "id": pid,
                "name": nodes_global[pid]["name"],
                "events_during": n_events,
                "degree": int(G.degree(pid)) if pid in G else 0,
            }
        )
    phase_event_count.sort(key=lambda r: (-r["events_during"], r["id"]))

    isolated_phases = [r for r in phase_event_count if r["events_during"] <= 2]

    transforms_edges = [
        (src, tgt)
        for (src, tgt, etype) in edges_global
        if etype == "TRANSFORMS_INTO"
        and nodes_global.get(src, {}).get("type") == "Phase"
        and nodes_global.get(tgt, {}).get("type") == "Phase"
    ]
    DG: nx.DiGraph = nx.DiGraph()
    DG.add_nodes_from(pid for pid in phase_ids)
    DG.add_edges_from(transforms_edges)
    chains: list[list[dict[str, Any]]] = []
    for comp in nx.weakly_connected_components(DG):
        sub = DG.subgraph(comp)
        if sub.number_of_edges() == 0:
            continue
        try:
            order = list(nx.topological_sort(sub))
        except nx.NetworkXUnfeasible:
            order = sorted(comp)
        chains.append(
            [
                {"id": pid, "name": nodes_global[pid]["name"]}
                for pid in order
            ]
        )
    chains.sort(key=lambda c: (-len(c), c[0]["id"] if c else ""))

    top_events = [r for r in hubs["top_overall"] if r["type"] == "Event"]
    out_idx = _outgoing_index(edges_global)
    top_events_without_during = []
    for r in top_events:
        eid = r["id"]
        has_during = any(
            etype == "DURING" and nodes_global.get(tgt, {}).get("type") == "Phase"
            for tgt, etype in out_idx.get(eid, [])
        )
        if not has_during:
            top_events_without_during.append(r)

    return {
        "phase_total": len(phase_ids),
        "phase_event_count": phase_event_count,
        "isolated_phases_count": len(isolated_phases),
        "isolated_phases": isolated_phases,
        "transforms_edges_total": len(transforms_edges),
        "transforms_chains_count": len(chains),
        "transforms_chains": chains,
        "top_events_without_during": top_events_without_during,
    }


def fig_phase_events(phase_event_count: list[dict[str, Any]]) -> go.Figure:
    labels = [r["name"] for r in phase_event_count]
    values = [r["events_during"] for r in phase_event_count]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values,
                y=labels,
                orientation="h",
                text=values,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title=f"Phase ordinate per numero di Event ancorati via DURING ({len(labels)} Phase)",
        xaxis_title="numero di Event con DURING entrante",
        height=max(450, 22 * len(labels) + 200),
    )
    fig.update_yaxes(autorange="reversed")
    return fig


# ----------------------------------------------------------------------------
# Sezione 6: Reflection
# ----------------------------------------------------------------------------


def compute_reflections(
    nodes_global: dict[str, dict[str, Any]],
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
    reflections_per_chunk: dict[str, int],
) -> dict[str, Any]:
    out_idx = _outgoing_index(edges_global)

    reflection_ids = [nid for nid, rec in nodes_global.items() if rec["type"] == "Reflection"]

    orphans = []
    target_type_counter: Counter[str] = Counter()
    for rid in reflection_ids:
        outs_reflects = [
            (tgt, etype)
            for tgt, etype in out_idx.get(rid, [])
            if etype == "REFLECTS_ON"
        ]
        if not outs_reflects:
            orphans.append({"id": rid, "name": nodes_global[rid]["name"]})
        for tgt, _ in outs_reflects:
            target_type_counter[nodes_global.get(tgt, {}).get("type", "Unknown")] += 1

    counts = list(reflections_per_chunk.values())
    distribution = Counter(counts)
    distribution_sorted = sorted(distribution.items())
    mean = sum(counts) / len(counts) if counts else 0.0

    return {
        "reflection_total": len(reflection_ids),
        "reflections_per_chunk": reflections_per_chunk,
        "reflections_per_chunk_distribution": [
            {"reflections_in_chunk": k, "n_chunks": v} for k, v in distribution_sorted
        ],
        "reflections_per_chunk_mean": mean,
        "chunks_with_zero_reflections": [c for c, n in reflections_per_chunk.items() if n == 0],
        "orphan_reflections_count": len(orphans),
        "orphan_reflections": orphans,
        "reflects_on_target_types": dict(target_type_counter),
    }


def fig_reflections_per_chunk(reflections_per_chunk: dict[str, int]) -> go.Figure:
    counts = list(reflections_per_chunk.values())
    if not counts:
        return go.Figure()
    max_c = max(counts)
    fig = go.Figure(
        data=[
            go.Histogram(
                x=counts,
                xbins=dict(start=-0.5, end=max_c + 0.5, size=1),
            )
        ]
    )
    fig.update_layout(
        title=f"Distribuzione: numero di Reflection per chunk ({len(counts)} chunk)",
        xaxis_title="numero di Reflection nel chunk",
        yaxis_title="numero di chunk",
        bargap=0.05,
    )
    return fig


def fig_reflects_on_targets(target_types: dict[str, int]) -> go.Figure:
    items = sorted(target_types.items(), key=lambda x: -x[1])
    types = [t for t, _ in items]
    values = [v for _, v in items]
    fig = go.Figure(
        data=[
            go.Bar(
                x=types,
                y=values,
                text=values,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title="REFLECTS_ON: distribuzione per tipo del target",
        xaxis_title="tipo di nodo target",
        yaxis_title="numero di archi REFLECTS_ON",
        bargap=0.25,
    )
    return fig


# ----------------------------------------------------------------------------
# Sezione 7: Theme
# ----------------------------------------------------------------------------


def compute_themes(
    nodes_global: dict[str, dict[str, Any]],
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
    G: nx.Graph,
    top_n: int = DEFAULT_TOP_PER_TYPE,
) -> dict[str, Any]:
    in_idx = _incoming_index(edges_global)

    theme_ids = [nid for nid, rec in nodes_global.items() if rec["type"] == "Theme"]

    rows: list[dict[str, Any]] = []
    for tid in theme_ids:
        embodies_in = [
            src
            for src, etype in in_idx.get(tid, [])
            if etype == "EMBODIES"
            and nodes_global.get(src, {}).get("type") in ("Event", "Person")
        ]
        rows.append(
            {
                "id": tid,
                "name": nodes_global[tid]["name"],
                "embodies_in": len(embodies_in),
                "degree": int(G.degree(tid)) if tid in G else 0,
            }
        )
    rows.sort(key=lambda r: (-r["embodies_in"], -r["degree"], r["id"]))

    degree_one = [r for r in rows if r["degree"] == 1]
    top_themes = rows[:top_n]

    return {
        "theme_total": len(theme_ids),
        "themes_ranked": rows,
        "themes_degree_one_count": len(degree_one),
        "themes_degree_one": degree_one,
        "top_themes": top_themes,
    }


def fig_themes_ranked(rows: list[dict[str, Any]], top_n: int = 30) -> go.Figure:
    rows_top = rows[:top_n]
    labels = [r["name"] for r in rows_top]
    values = [r["embodies_in"] for r in rows_top]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values,
                y=labels,
                orientation="h",
                text=values,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title=f"Top {len(rows_top)} Theme per numero di EMBODIES entranti (Event+Person)",
        xaxis_title="numero di EMBODIES entranti",
        height=max(450, 22 * len(rows_top) + 200),
    )
    fig.update_yaxes(autorange="reversed")
    return fig


# ----------------------------------------------------------------------------
# Sezione 8: archi narrativi
# ----------------------------------------------------------------------------


def compute_narrative_arcs(
    edges_global: dict[tuple[str, str, str], dict[str, Any]],
    nodes_global: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    counts = {t: 0 for t in NARRATIVE_EDGE_TYPES}
    for (_, _, etype) in edges_global:
        if etype in counts:
            counts[etype] += 1

    echo_edges = [(s, t) for (s, t, etype) in edges_global if etype == "ECHOES"]
    echo_set = set(echo_edges)
    reciprocal_pairs: set[frozenset[str]] = set()
    for s, t in echo_edges:
        if (t, s) in echo_set and s != t:
            reciprocal_pairs.add(frozenset({s, t}))
    echo_pairs = [
        {
            "members": [
                {"id": nid, "name": nodes_global[nid]["name"]}
                for nid in sorted(pair)
            ]
        }
        for pair in reciprocal_pairs
    ]

    caused = counts["CAUSED"]
    follows = counts["FOLLOWS"]
    if follows > 0:
        caused_follows_ratio = caused / follows
    elif caused > 0:
        caused_follows_ratio = float("inf")
    else:
        caused_follows_ratio = None

    return {
        "counts": counts,
        "echoes_reciprocal_pairs_count": len(echo_pairs),
        "echoes_reciprocal_pairs": echo_pairs,
        "caused_follows_ratio": caused_follows_ratio,
    }


def fig_narrative_arcs(counts: dict[str, int]) -> go.Figure:
    types = list(NARRATIVE_EDGE_TYPES)
    values = [counts.get(t, 0) for t in types]
    fig = go.Figure(
        data=[
            go.Bar(
                x=types,
                y=values,
                text=values,
                textposition="outside",
            )
        ]
    )
    fig.update_layout(
        title="Conteggi degli archi narrativi",
        yaxis_title="numero di archi",
        bargap=0.25,
    )
    return fig


# ----------------------------------------------------------------------------
# Sezione 9: provenienza (confidence per-occorrenza, chunk vuoti)
# ----------------------------------------------------------------------------


DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.5
CONFIDENCE_THRESHOLDS: tuple[float, ...] = (0.5, 0.7, 0.9)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _confidence_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "missing": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "p10": None,
            "p25": None,
            "p75": None,
            "p90": None,
            "below_thresholds": {f"<{t}": 0 for t in CONFIDENCE_THRESHOLDS},
        }
    return {
        "count": len(values),
        "missing": 0,
        "mean": sum(values) / len(values),
        "median": _median(values),
        "min": min(values),
        "max": max(values),
        "p10": _quantile(values, 0.10),
        "p25": _quantile(values, 0.25),
        "p75": _quantile(values, 0.75),
        "p90": _quantile(values, 0.90),
        "below_thresholds": {
            f"<{t}": sum(1 for v in values if v < t) for t in CONFIDENCE_THRESHOLDS
        },
    }


def compute_provenance(
    node_occurrences: list[dict[str, Any]],
    edge_occurrences: list[dict[str, Any]],
    empty_chunk_ids: list[str],
    low_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
    top_low_n: int = 30,
) -> dict[str, Any]:
    """Distribuzione delle confidence dichiarate dall'LLM e chunk vuoti."""
    node_conf = [o["confidence"] for o in node_occurrences if isinstance(o["confidence"], (int, float))]
    edge_conf = [o["confidence"] for o in edge_occurrences if isinstance(o["confidence"], (int, float))]

    node_missing = sum(1 for o in node_occurrences if not isinstance(o["confidence"], (int, float)))
    edge_missing = sum(1 for o in edge_occurrences if not isinstance(o["confidence"], (int, float)))

    node_stats = _confidence_stats(node_conf)
    node_stats["missing"] = node_missing
    edge_stats = _confidence_stats(edge_conf)
    edge_stats["missing"] = edge_missing

    low_nodes_all = [
        o
        for o in node_occurrences
        if isinstance(o["confidence"], (int, float)) and o["confidence"] < low_threshold
    ]
    low_nodes_all.sort(key=lambda o: (o["confidence"], o["id"]))

    low_edges_all = [
        o
        for o in edge_occurrences
        if isinstance(o["confidence"], (int, float)) and o["confidence"] < low_threshold
    ]
    low_edges_all.sort(key=lambda o: (o["confidence"], o["source_id"], o["target_id"]))

    low_nodes_by_type: Counter[str] = Counter(o["type"] for o in low_nodes_all)
    low_edges_by_type: Counter[str] = Counter(o["type"] for o in low_edges_all)

    def _trim_span(s: Any, n: int = 140) -> str:
        if not isinstance(s, str):
            return ""
        return s if len(s) <= n else s[: n - 1] + "…"

    low_nodes_top = [
        {
            "confidence": round(o["confidence"], 3),
            "id": o["id"],
            "type": o["type"],
            "name": o["name"],
            "chunk_id": o["chunk_id"],
            "evidence_span": _trim_span(o["evidence_span"]),
        }
        for o in low_nodes_all[:top_low_n]
    ]
    low_edges_top = [
        {
            "confidence": round(o["confidence"], 3),
            "source_id": o["source_id"],
            "target_id": o["target_id"],
            "type": o["type"],
            "chunk_id": o["chunk_id"],
            "evidence_span": _trim_span(o["evidence_span"]),
        }
        for o in low_edges_all[:top_low_n]
    ]

    return {
        "low_threshold": low_threshold,
        "node_confidence": node_stats,
        "edge_confidence": edge_stats,
        "low_nodes_count": len(low_nodes_all),
        "low_edges_count": len(low_edges_all),
        "low_nodes_by_type": dict(low_nodes_by_type),
        "low_edges_by_type": dict(low_edges_by_type),
        "low_nodes_top": low_nodes_top,
        "low_edges_top": low_edges_top,
        "empty_chunks_count": len(empty_chunk_ids),
        "empty_chunks": sorted(empty_chunk_ids),
        "_node_confidence_values": node_conf,
        "_edge_confidence_values": edge_conf,
    }


def fig_confidence_histogram(prov: dict[str, Any]) -> go.Figure:
    """Istogramma sovrapposto delle confidence per occorrenze di nodi e archi."""
    node_vals = prov["_node_confidence_values"]
    edge_vals = prov["_edge_confidence_values"]
    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=node_vals,
            name=f"nodi (n={len(node_vals)})",
            xbins=dict(start=0.0, end=1.0, size=0.05),
            opacity=0.65,
        )
    )
    fig.add_trace(
        go.Histogram(
            x=edge_vals,
            name=f"archi (n={len(edge_vals)})",
            xbins=dict(start=0.0, end=1.0, size=0.05),
            opacity=0.65,
        )
    )
    fig.add_vline(
        x=prov["low_threshold"],
        line_dash="dash",
        line_color="#d97706",
        annotation_text=f"soglia bassa {prov['low_threshold']}",
        annotation_position="top right",
    )
    fig.update_layout(
        title="Distribuzione confidence per occorrenza (nodi vs archi)",
        xaxis_title="confidence dichiarata dal modello",
        yaxis_title="numero di occorrenze",
        barmode="overlay",
        bargap=0.02,
    )
    return fig


def fig_confidence_thresholds(prov: dict[str, Any]) -> go.Figure:
    """Bar conteggi delle occorrenze sotto-soglia (0.5, 0.7, 0.9)."""
    thresholds = [f"<{t}" for t in CONFIDENCE_THRESHOLDS]
    node_counts = [prov["node_confidence"]["below_thresholds"][t] for t in thresholds]
    edge_counts = [prov["edge_confidence"]["below_thresholds"][t] for t in thresholds]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=thresholds,
            y=node_counts,
            text=node_counts,
            textposition="outside",
            name=f"nodi (totale {prov['node_confidence']['count']})",
        )
    )
    fig.add_trace(
        go.Bar(
            x=thresholds,
            y=edge_counts,
            text=edge_counts,
            textposition="outside",
            name=f"archi (totale {prov['edge_confidence']['count']})",
        )
    )
    fig.update_layout(
        title="Occorrenze sotto-soglia di confidence",
        yaxis_title="numero di occorrenze",
        barmode="group",
        bargap=0.25,
    )
    return fig


def fig_low_confidence_by_type(prov: dict[str, Any]) -> go.Figure:
    """Composizione per tipo dei nodi/archi sotto la soglia configurata."""
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=(
            f"Nodi con confidence < {prov['low_threshold']} ({prov['low_nodes_count']})",
            f"Archi con confidence < {prov['low_threshold']} ({prov['low_edges_count']})",
        ),
        horizontal_spacing=0.18,
    )
    node_items = sorted(prov["low_nodes_by_type"].items(), key=lambda x: -x[1])
    edge_items = sorted(prov["low_edges_by_type"].items(), key=lambda x: -x[1])
    fig.add_trace(
        go.Bar(
            x=[t for t, _ in node_items],
            y=[v for _, v in node_items],
            text=[v for _, v in node_items],
            textposition="outside",
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=[t for t, _ in edge_items],
            y=[v for _, v in edge_items],
            text=[v for _, v in edge_items],
            textposition="outside",
            showlegend=False,
        ),
        row=1,
        col=2,
    )
    fig.update_layout(
        title="Composizione delle occorrenze sotto-soglia per tipo",
        bargap=0.25,
        height=420,
    )
    return fig


# ----------------------------------------------------------------------------
# Serializzazione per metrics.json (set -> list ordinata)
# ----------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ----------------------------------------------------------------------------
# Rendering del report HTML
# ----------------------------------------------------------------------------


HTML_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 1180px; margin: 2rem auto; padding: 0 1rem;
         color: #1f2933; line-height: 1.45; }
  h1 { border-bottom: 2px solid #1f2933; padding-bottom: .4rem; }
  h2 { margin-top: 2.4rem; border-bottom: 1px solid #cbd2d9;
       padding-bottom: .3rem; }
  h3 { margin-top: 1.6rem; }
  .meta { background: #f5f7fa; padding: .8rem 1rem; border-radius: 6px;
          font-size: .95em; }
  table { border-collapse: collapse; margin: .8rem 0;
          font-size: .92em; }
  th, td { border: 1px solid #cbd2d9; padding: 4px 10px; text-align: left;
           vertical-align: top; }
  th { background: #f5f7fa; }
  details { margin: .5rem 0; }
  summary { cursor: pointer; font-weight: 600; color: #334e68; }
  .warn { background: #fff4e5; border-left: 4px solid #d97706;
          padding: .6rem .8rem; margin: .8rem 0; font-size: .92em; }
  .nav a { margin-right: 1rem; }
  .small { color: #486581; font-size: .9em; }
  .num { text-align: right; }
</style>
"""


def _html_escape(s: Any) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """rows: lista di dict; columns: lista di (key, header)."""
    if not rows:
        return '<p class="small">— nessun elemento —</p>'
    head = "".join(f"<th>{_html_escape(h)}</th>" for _, h in columns)
    body = []
    for r in rows:
        cells = "".join(f"<td>{_html_escape(r.get(k, ''))}</td>" for k, _ in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _details(summary: str, content: str, open_: bool = False) -> str:
    op = " open" if open_ else ""
    return f"<details{op}><summary>{_html_escape(summary)}</summary>{content}</details>"


def _fig_html(fig: go.Figure, include_js: bool) -> str:
    return fig.to_html(
        include_plotlyjs="cdn" if include_js else False,
        full_html=False,
        config={"displaylogo": False},
    )


def render_report(
    header: dict[str, Any],
    counts: dict[str, Any],
    hubs: dict[str, Any],
    isolation: dict[str, Any],
    event_quality: dict[str, Any],
    phases: dict[str, Any],
    reflections: dict[str, Any],
    themes: dict[str, Any],
    arcs: dict[str, Any],
    provenance: dict[str, Any],
    warnings: list[str],
    input_path: Path,
    adriano_id: str,
) -> str:
    figs_node = fig_node_types(counts)
    figs_edge = fig_edge_types(counts)
    figs_top_overall = fig_top_overall(hubs["top_overall"])
    figs_top_by_type = fig_top_by_type(hubs["top_by_type"])
    figs_component_sizes = fig_component_sizes(isolation["component_sizes"])
    figs_event_quality = fig_event_quality_summary(event_quality)
    figs_phases = fig_phase_events(phases["phase_event_count"])
    figs_reflections = fig_reflections_per_chunk(reflections["reflections_per_chunk"])
    figs_reflects_targets = fig_reflects_on_targets(reflections["reflects_on_target_types"])
    figs_themes = fig_themes_ranked(themes["themes_ranked"], top_n=30)
    figs_arcs = fig_narrative_arcs(arcs["counts"])
    figs_conf_hist = fig_confidence_histogram(provenance)
    figs_conf_thresh = fig_confidence_thresholds(provenance)
    figs_conf_types = fig_low_confidence_by_type(provenance)

    parts: list[str] = []
    parts.append("<!DOCTYPE html><html lang='it'><head>")
    parts.append("<meta charset='utf-8'>")
    parts.append("<title>Extraction analysis — KG biografico</title>")
    parts.append(HTML_STYLE)
    parts.append("</head><body>")

    parts.append("<h1>Extraction analysis — KG biografico</h1>")
    parts.append("<div class='meta'>")
    parts.append(f"<div><b>Input</b>: <code>{_html_escape(input_path)}</code></div>")
    parts.append(f"<div><b>Adriano id</b>: <code>{_html_escape(adriano_id)}</code></div>")
    if header:
        meta_items = []
        for k in ("source", "created_at", "stage_version", "schema_version",
                  "prompt_version", "model", "mode", "total_chunks_processed"):
            if k in header:
                meta_items.append(f"<b>{_html_escape(k)}</b>: {_html_escape(header[k])}")
        if meta_items:
            parts.append("<div>" + " &nbsp;·&nbsp; ".join(meta_items) + "</div>")
    parts.append("</div>")

    nav = [
        ("#sec-counts", "1. Conteggi"),
        ("#sec-hubs", "2. Hub"),
        ("#sec-isolation", "3. Isolamento"),
        ("#sec-events", "4. Qualità Event"),
        ("#sec-phases", "5. Phase"),
        ("#sec-reflections", "6. Reflection"),
        ("#sec-themes", "7. Theme"),
        ("#sec-arcs", "8. Archi narrativi"),
        ("#sec-provenance", "9. Provenienza"),
    ]
    parts.append("<p class='nav'>" + " ".join(f"<a href='{href}'>{label}</a>" for href, label in nav) + "</p>")

    if warnings:
        parts.append("<div class='warn'><b>Avvertenze</b><ul>")
        for w in warnings[:20]:
            parts.append(f"<li>{_html_escape(w)}</li>")
        if len(warnings) > 20:
            parts.append(f"<li>… e altre {len(warnings) - 20} avvertenze (vedi metrics.json)</li>")
        parts.append("</ul></div>")

    # Sezione 1
    parts.append("<h2 id='sec-counts'>1. Conteggi e distribuzioni</h2>")
    parts.append(
        f"<p>Nodi totali: <b>{counts['nodes_total']}</b>. "
        f"Archi (triple uniche source/target/type): <b>{counts['edges_total']}</b>.</p>"
    )
    parts.append(_fig_html(figs_node, include_js=True))
    parts.append(_fig_html(figs_edge, include_js=False))
    if counts["by_node_type_not_in_schema"] or counts["by_edge_type_not_in_schema"]:
        parts.append("<div class='warn'>")
        if counts["by_node_type_not_in_schema"]:
            parts.append(
                f"<div>Tipi di nodo fuori schema: <code>{_html_escape(counts['by_node_type_not_in_schema'])}</code></div>"
            )
        if counts["by_edge_type_not_in_schema"]:
            parts.append(
                f"<div>Tipi di arco fuori schema: <code>{_html_escape(counts['by_edge_type_not_in_schema'])}</code></div>"
            )
        parts.append("</div>")

    # Sezione 2
    parts.append("<h2 id='sec-hubs'>2. Hub e centralità</h2>")
    parts.append(f"<h3>Top {len(hubs['top_overall'])} nodi per grado (vista non orientata)</h3>")
    parts.append(_fig_html(figs_top_overall, include_js=False))
    parts.append(
        _details(
            "Tabella top by degree",
            _table(
                hubs["top_overall"],
                [("id", "id"), ("name", "name"), ("type", "type"), ("degree", "degree")],
            ),
        )
    )
    parts.append("<h3>Top per tipo (Person / Place / Theme / Phase)</h3>")
    parts.append(_fig_html(figs_top_by_type, include_js=False))

    parts.append("<h3>Anomalie di grado — Person / Place con un solo arco</h3>")
    parts.append(
        f"<p>Person a grado 1: <b>{len(hubs['degree_one_person'])}</b>; "
        f"Place a grado 1: <b>{len(hubs['degree_one_place'])}</b>.</p>"
    )
    parts.append(
        _details(
            f"Person con grado 1 ({len(hubs['degree_one_person'])})",
            _table(hubs["degree_one_person"], [("id", "id"), ("name", "name"), ("degree", "degree")]),
        )
    )
    parts.append(
        _details(
            f"Place con grado 1 ({len(hubs['degree_one_place'])})",
            _table(hubs["degree_one_place"], [("id", "id"), ("name", "name"), ("degree", "degree")]),
        )
    )

    # Sezione 3
    parts.append("<h2 id='sec-isolation'>3. Isolamento e connettività</h2>")
    parts.append(
        f"<p>Nodi isolati (degree 0): <b>{isolation['isolated_total']}</b>. "
        f"Nodi connessi <i>solo</i> ad Adriano (degree 1, vicino = <code>{_html_escape(adriano_id)}</code>): "
        f"<b>{isolation['orphans_to_adriano_total']}</b>. "
        f"Componenti connesse: <b>{isolation['components_count']}</b>, "
        f"componente gigante: <b>{isolation['giant_component_size']}</b> nodi "
        f"({isolation['giant_component_fraction']:.1%} dei nodi).</p>"
    )
    parts.append(_fig_html(figs_component_sizes, include_js=False))
    parts.append(
        _details(
            f"Nodi isolati ({isolation['isolated_total']})",
            _table(isolation["isolated"], [("id", "id"), ("name", "name"), ("type", "type")]),
        )
    )
    parts.append(
        _details(
            f"Orfani-rispetto-ad-Adriano ({isolation['orphans_to_adriano_total']})",
            _table(isolation["orphans_to_adriano"], [("id", "id"), ("name", "name"), ("type", "type")]),
        )
    )
    if isolation["small_components"]:
        small_html = []
        for i, comp in enumerate(isolation["small_components"], start=1):
            small_html.append(f"<h4>Componente {i + 1} — {comp['size']} nodi</h4>")
            small_html.append(
                _table(comp["members"], [("id", "id"), ("name", "name"), ("type", "type")])
            )
        parts.append(
            _details(
                f"Componenti non giganti ({len(isolation['small_components'])})",
                "".join(small_html),
            )
        )

    # Sezione 4
    parts.append("<h2 id='sec-events'>4. Qualità degli Event</h2>")
    parts.append(f"<p>Event totali: <b>{event_quality['event_total']}</b>.</p>")
    parts.append(_fig_html(figs_event_quality, include_js=False))
    quality_buckets = [
        ("missing_place", "Event senza LOCATED_AT"),
        ("missing_phase", "Event senza DURING"),
        ("missing_other_person", "Event senza altra Person oltre Adriano"),
        ("missing_theme", "Event senza EMBODIES Theme"),
        ("only_adriano", "Event con INVOLVES solo verso Adriano"),
        ("high_degree", f"Event con grado > {event_quality['high_degree_threshold']}"),
    ]
    for key, label in quality_buckets:
        lst = event_quality[key]
        parts.append(
            _details(
                f"{label} ({len(lst)})",
                _table(lst, [("id", "id"), ("name", "name"), ("degree", "degree"), ("chunks", "chunks")]),
            )
        )

    # Sezione 5
    parts.append("<h2 id='sec-phases'>5. Phase come spina dorsale</h2>")
    parts.append(
        f"<p>Phase totali: <b>{phases['phase_total']}</b>. "
        f"Phase con ≤2 Event ancorati: <b>{phases['isolated_phases_count']}</b>. "
        f"Archi TRANSFORMS_INTO Phase→Phase: <b>{phases['transforms_edges_total']}</b> "
        f"in <b>{phases['transforms_chains_count']}</b> catene.</p>"
    )
    parts.append(_fig_html(figs_phases, include_js=False))
    parts.append(
        _details(
            f"Phase isolate (≤2 Event) ({phases['isolated_phases_count']})",
            _table(
                phases["isolated_phases"],
                [("id", "id"), ("name", "name"), ("events_during", "events_during"), ("degree", "degree")],
            ),
        )
    )

    chains_html: list[str] = []
    if phases["transforms_chains"]:
        for i, chain in enumerate(phases["transforms_chains"], start=1):
            seq = " → ".join(_html_escape(item["name"]) for item in chain)
            chains_html.append(f"<div><b>Catena {i}</b> ({len(chain)} Phase): {seq}</div>")
    else:
        chains_html.append("<p class='small'>— nessuna catena TRANSFORMS_INTO trovata —</p>")
    parts.append(_details(f"Catene TRANSFORMS_INTO ({phases['transforms_chains_count']})", "".join(chains_html)))

    parts.append(
        _details(
            f"Top Event by degree senza DURING ({len(phases['top_events_without_during'])})",
            _table(
                phases["top_events_without_during"],
                [("id", "id"), ("name", "name"), ("degree", "degree")],
            ),
        )
    )

    # Sezione 6
    parts.append("<h2 id='sec-reflections'>6. Reflection</h2>")
    parts.append(
        f"<p>Reflection totali: <b>{reflections['reflection_total']}</b>. "
        f"Media per chunk: <b>{reflections['reflections_per_chunk_mean']:.2f}</b>. "
        f"Chunk senza alcuna Reflection: <b>{len(reflections['chunks_with_zero_reflections'])}</b>. "
        f"Reflection orfane (senza REFLECTS_ON in uscita): <b>{reflections['orphan_reflections_count']}</b>.</p>"
    )
    parts.append(_fig_html(figs_reflections, include_js=False))
    parts.append(_fig_html(figs_reflects_targets, include_js=False))
    parts.append(
        _details(
            f"Reflection orfane ({reflections['orphan_reflections_count']})",
            _table(reflections["orphan_reflections"], [("id", "id"), ("name", "name")]),
        )
    )
    parts.append(
        _details(
            "Distribuzione Reflection-per-chunk (tabella)",
            _table(
                reflections["reflections_per_chunk_distribution"],
                [("reflections_in_chunk", "Reflection nel chunk"), ("n_chunks", "numero di chunk")],
            ),
        )
    )

    # Sezione 7
    parts.append("<h2 id='sec-themes'>7. Theme</h2>")
    parts.append(
        f"<p>Theme totali: <b>{themes['theme_total']}</b>. "
        f"Theme citati una sola volta (degree 1): <b>{themes['themes_degree_one_count']}</b>.</p>"
    )
    parts.append(_fig_html(figs_themes, include_js=False))
    parts.append(
        _details(
            f"Top {len(themes['top_themes'])} Theme",
            _table(
                themes["top_themes"],
                [("id", "id"), ("name", "name"), ("embodies_in", "embodies_in"), ("degree", "degree")],
            ),
        )
    )
    parts.append(
        _details(
            f"Theme con degree 1 ({themes['themes_degree_one_count']})",
            _table(
                themes["themes_degree_one"],
                [("id", "id"), ("name", "name"), ("embodies_in", "embodies_in"), ("degree", "degree")],
            ),
        )
    )
    parts.append(
        _details(
            "Tutti i Theme ordinati",
            _table(
                themes["themes_ranked"],
                [("id", "id"), ("name", "name"), ("embodies_in", "embodies_in"), ("degree", "degree")],
            ),
        )
    )

    # Sezione 8
    parts.append("<h2 id='sec-arcs'>8. Archi narrativi</h2>")
    counts_arcs = arcs["counts"]
    ratio = arcs["caused_follows_ratio"]
    if ratio is None:
        ratio_str = "n/a (zero CAUSED e zero FOLLOWS)"
    elif ratio == float("inf"):
        ratio_str = "∞ (CAUSED > 0, FOLLOWS = 0)"
    else:
        ratio_str = f"{ratio:.2f}"
    parts.append(
        f"<p>ECHOES: <b>{counts_arcs['ECHOES']}</b> archi, "
        f"di cui <b>{arcs['echoes_reciprocal_pairs_count']}</b> coppie reciproche. "
        f"CAUSED: <b>{counts_arcs['CAUSED']}</b>, FOLLOWS: <b>{counts_arcs['FOLLOWS']}</b>, "
        f"rapporto CAUSED/FOLLOWS = <b>{ratio_str}</b>. "
        f"CONTRASTS_WITH: <b>{counts_arcs['CONTRASTS_WITH']}</b>. "
        f"TRANSFORMS_INTO: <b>{counts_arcs['TRANSFORMS_INTO']}</b>. "
        f"RELATED_TO: <b>{counts_arcs['RELATED_TO']}</b>.</p>"
    )
    parts.append(_fig_html(figs_arcs, include_js=False))
    if arcs["echoes_reciprocal_pairs"]:
        rows = [
            {
                "a": pair["members"][0]["name"] + f" ({pair['members'][0]['id']})",
                "b": pair["members"][1]["name"] + f" ({pair['members'][1]['id']})"
                if len(pair["members"]) > 1
                else "",
            }
            for pair in arcs["echoes_reciprocal_pairs"]
        ]
        parts.append(
            _details(
                f"Coppie reciproche ECHOES ({len(rows)})",
                _table(rows, [("a", "nodo A"), ("b", "nodo B")]),
            )
        )

    # Sezione 9
    parts.append("<h2 id='sec-provenance'>9. Provenienza</h2>")
    parts.append(
        "<p class='small'>Le statistiche di provenance sono calcolate su tutte le "
        "<b>occorrenze per-chunk</b> (un nodo presente in N chunk pesa N volte), "
        "perché ogni estrazione è una decisione separata del modello.</p>"
    )
    nc = provenance["node_confidence"]
    ec = provenance["edge_confidence"]

    def _fmt(v: Any) -> str:
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.3f}"
        return _html_escape(v)

    stats_rows = [
        {
            "kind": "nodi",
            "count": nc["count"],
            "missing": nc["missing"],
            "mean": _fmt(nc["mean"]),
            "median": _fmt(nc["median"]),
            "min": _fmt(nc["min"]),
            "p10": _fmt(nc["p10"]),
            "p25": _fmt(nc["p25"]),
            "p75": _fmt(nc["p75"]),
            "p90": _fmt(nc["p90"]),
            "max": _fmt(nc["max"]),
        },
        {
            "kind": "archi",
            "count": ec["count"],
            "missing": ec["missing"],
            "mean": _fmt(ec["mean"]),
            "median": _fmt(ec["median"]),
            "min": _fmt(ec["min"]),
            "p10": _fmt(ec["p10"]),
            "p25": _fmt(ec["p25"]),
            "p75": _fmt(ec["p75"]),
            "p90": _fmt(ec["p90"]),
            "max": _fmt(ec["max"]),
        },
    ]
    parts.append(
        _table(
            stats_rows,
            [
                ("kind", ""),
                ("count", "occorrenze"),
                ("missing", "senza confidence"),
                ("mean", "media"),
                ("median", "mediana"),
                ("min", "min"),
                ("p10", "p10"),
                ("p25", "p25"),
                ("p75", "p75"),
                ("p90", "p90"),
                ("max", "max"),
            ],
        )
    )
    parts.append(
        f"<p>Soglia bassa: <b>{provenance['low_threshold']}</b>. "
        f"Occorrenze sotto soglia — nodi: <b>{provenance['low_nodes_count']}</b>, "
        f"archi: <b>{provenance['low_edges_count']}</b>. "
        f"Chunk con zero estrazioni (nodi e archi entrambi vuoti): "
        f"<b>{provenance['empty_chunks_count']}</b>.</p>"
    )
    parts.append(_fig_html(figs_conf_hist, include_js=False))
    parts.append(_fig_html(figs_conf_thresh, include_js=False))
    parts.append(_fig_html(figs_conf_types, include_js=False))

    parts.append(
        _details(
            f"Nodi con confidence < {provenance['low_threshold']} — primi {len(provenance['low_nodes_top'])}",
            _table(
                provenance["low_nodes_top"],
                [
                    ("confidence", "conf"),
                    ("type", "type"),
                    ("name", "name"),
                    ("id", "id"),
                    ("chunk_id", "chunk"),
                    ("evidence_span", "evidence_span"),
                ],
            ),
        )
    )
    parts.append(
        _details(
            f"Archi con confidence < {provenance['low_threshold']} — primi {len(provenance['low_edges_top'])}",
            _table(
                provenance["low_edges_top"],
                [
                    ("confidence", "conf"),
                    ("type", "type"),
                    ("source_id", "source"),
                    ("target_id", "target"),
                    ("chunk_id", "chunk"),
                    ("evidence_span", "evidence_span"),
                ],
            ),
        )
    )
    if provenance["empty_chunks"]:
        empty_rows = [{"chunk_id": c} for c in provenance["empty_chunks"]]
        parts.append(
            _details(
                f"Chunk con zero estrazioni ({provenance['empty_chunks_count']})",
                _table(empty_rows, [("chunk_id", "chunk_id")]),
            )
        )

    parts.append("</body></html>")
    return "".join(parts)


# ----------------------------------------------------------------------------
# Stampa riassuntiva su stdout
# ----------------------------------------------------------------------------


def print_summary(
    counts: dict[str, Any],
    hubs: dict[str, Any],
    isolation: dict[str, Any],
    event_quality: dict[str, Any],
    phases: dict[str, Any],
    reflections: dict[str, Any],
    themes: dict[str, Any],
    arcs: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    print("\n=== Conteggi ===")
    print(f"Nodi totali: {counts['nodes_total']}  | Archi totali: {counts['edges_total']}")
    print(f"  Nodi per tipo: {counts['by_node_type']}")
    if counts["by_node_type_not_in_schema"]:
        print(f"  Nodi fuori schema: {counts['by_node_type_not_in_schema']}")
    print(f"  Archi per tipo: {counts['by_edge_type']}")
    if counts["by_edge_type_not_in_schema"]:
        print(f"  Archi fuori schema: {counts['by_edge_type_not_in_schema']}")

    print("\n=== Top 10 by degree ===")
    for r in hubs["top_overall"][:10]:
        print(f"  {r['degree']:>3} | {r['name']} [{r['type']}] ({r['id']})")

    print("\n=== Isolamento e componenti ===")
    print(
        f"  isolati: {isolation['isolated_total']} | orfani->Adriano: {isolation['orphans_to_adriano_total']} | "
        f"componenti: {isolation['components_count']} | gigante: {isolation['giant_component_size']} "
        f"({isolation['giant_component_fraction']:.1%})"
    )

    print("\n=== Qualità Event ===")
    eq = event_quality
    print(
        f"  Event: {eq['event_total']} | senza LOCATED_AT: {eq['missing_place_count']} | "
        f"senza DURING: {eq['missing_phase_count']} | senza altra Person: {eq['missing_other_person_count']} | "
        f"senza Theme: {eq['missing_theme_count']} | solo Adriano: {eq['only_adriano_count']} | "
        f"grado > {eq['high_degree_threshold']}: {eq['high_degree_count']}"
    )

    print("\n=== Phase ===")
    print(
        f"  Phase: {phases['phase_total']} | con <=2 Event: {phases['isolated_phases_count']} | "
        f"TRANSFORMS_INTO archi: {phases['transforms_edges_total']} | catene: {phases['transforms_chains_count']}"
    )

    print("\n=== Reflection ===")
    print(
        f"  Reflection: {reflections['reflection_total']} | "
        f"media per chunk: {reflections['reflections_per_chunk_mean']:.2f} | "
        f"chunk con 0 Reflection: {len(reflections['chunks_with_zero_reflections'])} | "
        f"orfane: {reflections['orphan_reflections_count']}"
    )
    print(f"  REFLECTS_ON target types: {reflections['reflects_on_target_types']}")

    print("\n=== Theme ===")
    print(f"  Theme: {themes['theme_total']} | degree 1: {themes['themes_degree_one_count']}")
    for r in themes["top_themes"][:10]:
        print(f"  {r['embodies_in']:>3} | {r['name']} ({r['id']})")

    print("\n=== Archi narrativi ===")
    print(f"  {arcs['counts']}")
    print(
        f"  ECHOES coppie reciproche: {arcs['echoes_reciprocal_pairs_count']} | "
        f"CAUSED/FOLLOWS: {arcs['caused_follows_ratio']}"
    )

    print("\n=== Provenienza ===")
    nc = provenance["node_confidence"]
    ec = provenance["edge_confidence"]

    def _f(v: Any) -> str:
        if v is None:
            return "n/a"
        if isinstance(v, float):
            return f"{v:.3f}"
        return str(v)

    print(
        f"  Nodi (occorrenze {nc['count']}, senza conf {nc['missing']}): "
        f"media {_f(nc['mean'])} | mediana {_f(nc['median'])} | "
        f"min {_f(nc['min'])} | p10 {_f(nc['p10'])} | p90 {_f(nc['p90'])}"
    )
    print(
        f"  Archi (occorrenze {ec['count']}, senza conf {ec['missing']}): "
        f"media {_f(ec['mean'])} | mediana {_f(ec['median'])} | "
        f"min {_f(ec['min'])} | p10 {_f(ec['p10'])} | p90 {_f(ec['p90'])}"
    )
    print(
        f"  Sotto-soglia nodi: {nc['below_thresholds']} | "
        f"archi: {ec['below_thresholds']}"
    )
    print(
        f"  Occorrenze sotto la soglia bassa {provenance['low_threshold']}: "
        f"nodi {provenance['low_nodes_count']} | archi {provenance['low_edges_count']}"
    )
    if provenance["low_nodes_by_type"]:
        print(f"  Nodi low-confidence per tipo: {provenance['low_nodes_by_type']}")
    if provenance["low_edges_by_type"]:
        print(f"  Archi low-confidence per tipo: {provenance['low_edges_by_type']}")
    print(f"  Chunk con zero estrazioni: {provenance['empty_chunks_count']}")
    if provenance["empty_chunks"]:
        preview = provenance["empty_chunks"][:10]
        more = "" if len(provenance["empty_chunks"]) <= 10 else f" (+{len(provenance['empty_chunks']) - 10} altri)"
        print(f"    {preview}{more}")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def run(
    input_path: Path,
    output_dir: Path,
    adriano_id: str,
    event_high_degree: int,
    low_confidence_threshold: float,
    print_to_stdout: bool,
    log_path: Path | None = None,
) -> None:
    header, extractions, resolved_log_path = load_extractions(input_path, log_path)
    (
        nodes_global,
        edges_global,
        refl_per_chunk,
        G,
        warnings,
        node_occurrences,
        edge_occurrences,
        empty_chunk_ids,
    ) = build_global_graph(extractions)

    counts = compute_counts(nodes_global, edges_global)
    hubs = compute_hubs(nodes_global=nodes_global, G=G)
    isolation = compute_isolation(G=G, nodes_global=nodes_global, adriano_id=adriano_id)
    event_quality = compute_event_quality(
        nodes_global=nodes_global,
        edges_global=edges_global,
        G=G,
        adriano_id=adriano_id,
        high_degree_threshold=event_high_degree,
    )
    phases = compute_phases(
        nodes_global=nodes_global, edges_global=edges_global, G=G, hubs=hubs
    )
    reflections = compute_reflections(
        nodes_global=nodes_global,
        edges_global=edges_global,
        reflections_per_chunk=refl_per_chunk,
    )
    themes = compute_themes(nodes_global=nodes_global, edges_global=edges_global, G=G)
    arcs = compute_narrative_arcs(edges_global=edges_global, nodes_global=nodes_global)
    provenance = compute_provenance(
        node_occurrences=node_occurrences,
        edge_occurrences=edge_occurrences,
        empty_chunk_ids=empty_chunk_ids,
        low_threshold=low_confidence_threshold,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.html"
    metrics_path = output_dir / "metrics.json"

    html = render_report(
        header=header,
        counts=counts,
        hubs=hubs,
        isolation=isolation,
        event_quality=event_quality,
        phases=phases,
        reflections=reflections,
        themes=themes,
        arcs=arcs,
        provenance=provenance,
        warnings=warnings,
        input_path=input_path,
        adriano_id=adriano_id,
    )
    report_path.write_text(html, encoding="utf-8")

    provenance_persist = {
        k: v for k, v in provenance.items() if not k.startswith("_")
    }
    metrics = {
        "input": str(input_path),
        "log": str(resolved_log_path),
        "header": header,
        "adriano_id": adriano_id,
        "event_high_degree_threshold": event_high_degree,
        "low_confidence_threshold": low_confidence_threshold,
        "counts": counts,
        "hubs": {k: v for k, v in hubs.items() if k != "by_id_degree"},
        "isolation": isolation,
        "event_quality": event_quality,
        "phases": phases,
        "reflections": {k: v for k, v in reflections.items() if k != "reflections_per_chunk"},
        "themes": themes,
        "narrative_arcs": arcs,
        "provenance": provenance_persist,
        "warnings": warnings,
    }
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    print(f"Report HTML scritto in: {report_path.resolve()}")
    print(f"Metrics JSON scritto in: {metrics_path.resolve()}")

    if print_to_stdout:
        print_summary(counts, hubs, isolation, event_quality, phases, reflections, themes, arcs, provenance)


def main() -> None:
    """
    Guida all'utilizzo
    ------------------

    Esempi (cwd = root della repo)::

        python Adriano_graph/tools/extraction_analysis.py
        python Adriano_graph/tools/extraction_analysis.py -i path/to/extracted_graph.json -o out_dir --print
        python Adriano_graph/tools/extraction_analysis.py --event-high-degree 12

    Output:
      - <output-dir>/report.html (self-contained, plotly.js da CDN)
      - <output-dir>/metrics.json (numeri grezzi)
    """
    ap = argparse.ArgumentParser(
        description="Analisi del knowledge graph estratto in stadio 3 (vista globale deduplicata).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=main.__doc__,
    )
    ap.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"File extracted_graph.json (default: {DEFAULT_INPUT})",
    )
    ap.add_argument(
        "-l",
        "--log",
        type=Path,
        default=None,
        help=(
            "File extraction_log.json con metadati di run. "
            "Default: sibling di --input nella stessa cartella."
        ),
    )
    ap.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Cartella di output (default: {DEFAULT_OUTPUT_DIR})",
    )
    ap.add_argument(
        "--adriano-id",
        type=str,
        default=DEFAULT_ADRIANO_ID,
        help=f"ID del nodo Adriano (default: {DEFAULT_ADRIANO_ID})",
    )
    ap.add_argument(
        "--event-high-degree",
        type=int,
        default=DEFAULT_EVENT_HIGH_DEGREE,
        help=f"Soglia di grado oltre la quale un Event è 'anomalmente denso' "
             f"(default: {DEFAULT_EVENT_HIGH_DEGREE})",
    )
    ap.add_argument(
        "--low-confidence",
        type=float,
        default=DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        help=f"Soglia di confidence sotto la quale un'occorrenza è considerata "
             f"'low confidence' (default: {DEFAULT_LOW_CONFIDENCE_THRESHOLD})",
    )
    ap.add_argument(
        "--print",
        dest="print_summary",
        action="store_true",
        help="Stampa un riassunto leggibile su stdout oltre al report HTML",
    )

    args = ap.parse_args()
    run(
        input_path=args.input,
        output_dir=args.output_dir,
        adriano_id=args.adriano_id,
        event_high_degree=args.event_high_degree,
        low_confidence_threshold=args.low_confidence,
        print_to_stdout=args.print_summary,
        log_path=args.log,
    )


if __name__ == "__main__":
    main()
