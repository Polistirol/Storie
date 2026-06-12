# src/stage_4_resolve.py
"""
Stadio 4 - Resolver: applica merge_map e split_map a extracted_graph.json
e produce due file separati:

  - data/stage_4/resolved_graph.json   il grafo deduplicato (SOLO nodi + archi)
  - data/stage_4/stage_4_log.json      il diario di bordo (merge applicati,
                                       split applicati, proposte stadio 6,
                                       warning, statistiche)

Cosa fa, in ordine:
  1) preflight: nessun canonical_id null in to_review, ecc.
  2) costruisce la rewrite map (id_originale, type) -> id_canonico,
     componendo split prima, merge dopo.
  3) accorpa i nodi per (id_canonico, type) raccogliendo TUTTE le provenances.
     description e name = piu' frequenti. aliases = unione.
  4) accorpa gli archi per (source, target, type, role). Role diverso resta
     arco distinto. Self-loop dopo merge: warning + skip.
  5) allinea source_type e target_type su ogni arco dai tipi dei nodi canonici.
  6) genera Stage6Proposal per gli split Event/Theme e Phase/Theme.
  7) valida ResolvedGraph Pydantic.
  8) scrive resolved_graph.json e stage_4_log.json.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.deduplication_schema import (
    DEDUP_SCHEMA_VERSION,
    MergeApplied,
    ResolvedEdge,
    ResolvedGraph,
    ResolvedNode,
    SplitApplied,
    Stage4Log,
    Stage6Proposal,
    Stats,
    Warning,
    align_edge_endpoint_types,
    edge_key,
)
from src.schema import EdgeType, NodeType, Provenance


#TYPE_COLLISIONS  = PROJECT_ROOT / "data" / "stage_4"/ "0_diagnostic" / "type_collisions.json"
#OUT_DIR = PROJECT_ROOT / "data" / "stage_4"/ "2_split"


INPUT_GRAPH = PROJECT_ROOT / "data" / "output" / "latest" / "extracted_graph.json"
MERGE_MAP = PROJECT_ROOT / "data" / "stage_4"/ "1_merge" / "merge_map.json"
SPLIT_MAP = PROJECT_ROOT / "data" / "stage_4"/ "2_split" / "split_map.json"
OUT_GRAPH = PROJECT_ROOT / "data" / "stage_4"/ "3_resolve" / "resolved_graph.json"
OUT_LOG= PROJECT_ROOT / "data" / "stage_4"/ "3_resolve" / "resolver_log.json"

STAGE_VERSION = "0.1.0"

# =============================================================================
# Preflight
# =============================================================================

def preflight_checks(graph: dict, merge_map: dict, split_map: dict) -> None:
    null_canonical = [
        d for d in merge_map.get("to_review", {}).get("decisions", [])
        if not d.get("canonical_id")
    ]
    if null_canonical:
        raise ValueError(
            f"merge_map.to_review contiene {len(null_canonical)} decisioni con "
            f"canonical_id null: vanno validate a mano. "
            f"Ids: {[d.get('merged_ids') for d in null_canonical[:5]]}..."
        )

    all_node_ids = {n["id"] for ext in graph["extractions"] for n in ext.get("nodes", [])}
    for d in _all_merge_decisions(merge_map):
        for loser in d.get("losers", []):
            if loser not in all_node_ids:
                raise ValueError(f"merge_map: loser {loser!r} non esiste fra i nodi del grafo")


def _all_merge_decisions(merge_map: dict) -> list[dict]:
    return (
        list(merge_map.get("auto", {}).get("decisions", []))
        + list(merge_map.get("to_review", {}).get("decisions", []))
    )


# =============================================================================
# Rewrite map
# =============================================================================

def build_rewrite_map(merge_map: dict, split_map: dict) -> tuple[dict, dict]:
    split_by_id: dict[str, dict[str, str]] = {
        d["original_id"]: dict(d["new_ids_by_type"])
        for d in split_map.get("decisions", [])
    }
    merge_by_id: dict[str, str] = {}
    for d in _all_merge_decisions(merge_map):
        canonical = d.get("canonical_id")
        if not canonical:
            continue
        for loser in d.get("losers", []):
            merge_by_id[loser] = canonical
    return split_by_id, merge_by_id


def resolve_id(original_id: str, ntype: str, split_by_id: dict, merge_by_id: dict) -> str:
    after_split = split_by_id.get(original_id, {}).get(ntype, original_id)
    return merge_by_id.get(after_split, after_split)


# =============================================================================
# Accorpamento nodi
# =============================================================================

def aggregate_nodes(graph: dict, split_by_id: dict, merge_by_id: dict,
                    merge_map: dict) -> list[ResolvedNode]:
    merge_info_by_canonical: dict[str, dict] = {}
    for d in _all_merge_decisions(merge_map):
        canonical = d.get("canonical_id")
        if canonical:
            merge_info_by_canonical[canonical] = {
                "method": d.get("method", "manual"),
                "confidence": d.get("confidence", 1.0),
                "merged_from": list(d.get("merged_ids") or [canonical]),
            }

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for extraction in graph["extractions"]:
        for node in extraction.get("nodes", []):
            final_id = resolve_id(node["id"], node["type"], split_by_id, merge_by_id)
            groups[(final_id, node["type"])].append(node)

    resolved: list[ResolvedNode] = []
    for (final_id, ntype), occurrences in groups.items():
        names = [n["name"] for n in occurrences if n.get("name")]
        descs = [n["description"] for n in occurrences if n.get("description")]

        chosen_name = Counter(names).most_common(1)[0][0] if names else final_id
        chosen_desc = Counter(descs).most_common(1)[0][0] if descs else None

        aliases: set[str] = set()
        for n in occurrences:
            aliases.update(n.get("aliases") or [])
            if n.get("name") and n["name"] != chosen_name:
                aliases.add(n["name"])
        if final_id in merge_info_by_canonical:
            for loser in merge_info_by_canonical[final_id]["merged_from"]:
                if loser != final_id:
                    aliases.add(loser)

        provenances = [Provenance(**n["provenance"]) for n in occurrences]
        merge_info = merge_info_by_canonical.get(final_id)

        resolved.append(ResolvedNode(
            id=final_id, type=ntype, name=chosen_name, description=chosen_desc,
            aliases=sorted(aliases), provenances=provenances,
            merged_from=merge_info["merged_from"] if merge_info else [final_id],
            merge_method=merge_info["method"] if merge_info else "none",
            merge_confidence=merge_info["confidence"] if merge_info else 1.0,
        ))

    resolved.sort(key=lambda n: (n.type.value, n.id))
    return resolved


# =============================================================================
# Accorpamento archi
# =============================================================================

def aggregate_edges(
    graph: dict,
    split_by_id: dict,
    merge_by_id: dict,
    node_type_by_id: dict[str, NodeType],
    warnings: list[Warning],
) -> list[ResolvedEdge]:
    orig_type: dict[tuple[str, str], str] = {}
    for extraction in graph["extractions"]:
        cid = extraction["chunk_id"]
        for n in extraction.get("nodes", []):
            orig_type[(n["id"], cid)] = n["type"]

    groups: dict[tuple[str, str, str, str | None], list[dict]] = defaultdict(list)
    for extraction in graph["extractions"]:
        cid = extraction["chunk_id"]
        for edge in extraction.get("edges", []):
            src_type = orig_type.get((edge["source_id"], cid))
            tgt_type = orig_type.get((edge["target_id"], cid))
            if src_type is None or tgt_type is None:
                warnings.append(Warning(
                    kind="orphan_edge",
                    detail=f"arco {edge['source_id']}->{edge['target_id']} in {cid} "
                           f"riferisce id senza nodo locale",
                ))
                continue
            src_final = resolve_id(edge["source_id"], src_type, split_by_id, merge_by_id)
            tgt_final = resolve_id(edge["target_id"], tgt_type, split_by_id, merge_by_id)
            key = (src_final, tgt_final, edge["type"], edge.get("role"))
            groups[key].append(edge)

    resolved: list[ResolvedEdge] = []
    for (src, tgt, etype, role), edges in groups.items():
        if src == tgt:
            warnings.append(Warning(
                kind="self_loop",
                detail=f"self-loop dopo merge: {src} type={etype}",
            ))
            continue

        descs = [e["description"] for e in edges if e.get("description")]
        chosen_desc = Counter(descs).most_common(1)[0][0] if descs else None
        provenances = [Provenance(**e["provenance"]) for e in edges]
        merged_from = sorted({edge_key(e) for e in edges})

        resolved.append(ResolvedEdge(
            source_id=src,
            target_id=tgt,
            type=EdgeType(etype),
            source_type=node_type_by_id[src],
            target_type=node_type_by_id[tgt],
            description=chosen_desc,
            role=role,
            provenances=provenances,
            merged_from=merged_from,
            merge_confidence=1.0,
        ))

    resolved.sort(key=lambda e: (e.source_id, e.type.value, e.target_id))
    return resolved


# =============================================================================
# Stage6Proposal
# =============================================================================

def build_stage6_proposals(split_map: dict) -> list[Stage6Proposal]:
    proposals: list[Stage6Proposal] = []
    for d in split_map.get("decisions", []):
        by_type = d["new_ids_by_type"]
        theme_id = by_type.get("Theme")
        if not theme_id:
            continue
        for src_type in ("Event", "Phase"):
            src_id = by_type.get(src_type)
            if src_id:
                proposals.append(Stage6Proposal(
                    kind="embodies_event_theme" if src_type == "Event" else "embodies_phase_theme",
                    source_id=src_id, target_id=theme_id, confidence=0.5,
                    note=f"split di id ambiguo {d['original_id']!r}: valutare arco EMBODIES",
                ))
    return proposals


# =============================================================================
# Main
# =============================================================================

def run() -> tuple[ResolvedGraph, Stage4Log]:
    graph = json.loads(INPUT_GRAPH.read_text(encoding="utf-8"))
    merge_map = json.loads(MERGE_MAP.read_text(encoding="utf-8"))
    split_map = json.loads(SPLIT_MAP.read_text(encoding="utf-8"))

    print("preflight checks...")
    preflight_checks(graph, merge_map, split_map)
    print("  ok")

    n_chunks = len(graph["extractions"])
    n_nodes_raw = sum(len(e.get("nodes", [])) for e in graph["extractions"])
    n_edges_raw = sum(len(e.get("edges", [])) for e in graph["extractions"])
    print(f"input: {n_chunks} chunk, {n_nodes_raw} occorrenze nodo, {n_edges_raw} occorrenze arco")

    split_by_id, merge_by_id = build_rewrite_map(merge_map, split_map)
    print(f"rewrite map: {len(split_by_id)} split, {len(merge_by_id)} merge (losers)")

    nodes = aggregate_nodes(graph, split_by_id, merge_by_id, merge_map)
    print(f"nodi accorpati: {len(nodes)}")

    node_type_by_id = {n.id: n.type for n in nodes}
    warnings: list[Warning] = []
    edges = aggregate_edges(graph, split_by_id, merge_by_id, node_type_by_id, warnings)
    print(f"archi accorpati: {len(edges)}  ({len(warnings)} warning)")

    proposals = build_stage6_proposals(split_map)
    print(f"proposte stadio 6: {len(proposals)}")

    # ResolvedGraph: solo nodi + archi
    source_run = str(INPUT_GRAPH)
    source_prompt_version = (graph.get("metadata") or {}).get("prompt_version")

    print("validazione ResolvedGraph...")
    resolved = ResolvedGraph(
        nodes=nodes, edges=edges,
        source_run=source_run, source_prompt_version=source_prompt_version,
        dedup_schema_version=DEDUP_SCHEMA_VERSION, stage_version=STAGE_VERSION,
        timestamp=datetime.now(timezone.utc),
    )
    print("  ok")

    print("allineamento source_type/target_type sugli archi...")
    resolved = align_edge_endpoint_types(resolved)
    print("  ok")

    # Stats
    prov_counts = sorted(len(n.provenances) for n in nodes)
    median = prov_counts[len(prov_counts) // 2] if prov_counts else 0
    p95 = prov_counts[int(len(prov_counts) * 0.95)] if prov_counts else 0
    top = sorted(nodes, key=lambda n: -len(n.provenances))[:5]
    stats = Stats(
        input_chunks=n_chunks,
        input_node_occurrences=n_nodes_raw,
        input_edge_occurrences=n_edges_raw,
        output_nodes=len(nodes),
        output_edges=len(edges),
        provenances_per_node_median=median,
        provenances_per_node_p95=p95,
        provenances_per_node_max=max(prov_counts) if prov_counts else 0,
        top_nodes=[{"id": n.id, "type": n.type.value, "n_provenances": len(n.provenances)} for n in top],
    )

    # Stage4Log: tutto il resto (proposte, merge applicati, split applicati, warning, stats)
    merges_applied = [
        MergeApplied(
            canonical_id=d["canonical_id"],
            losers=list(d.get("losers", [])),
            method=d.get("method", "manual"),
            confidence=d.get("confidence", 1.0),
        )
        for d in _all_merge_decisions(merge_map) if d.get("canonical_id")
    ]
    splits_applied = [
        SplitApplied(original_id=d["original_id"], new_ids_by_type=dict(d["new_ids_by_type"]))
        for d in split_map.get("decisions", [])
    ]
    log = Stage4Log(
        source_run=source_run,
        timestamp=datetime.now(timezone.utc),
        stage_version=STAGE_VERSION,
        dedup_schema_version=DEDUP_SCHEMA_VERSION,
        merges_applied=merges_applied,
        splits_applied=splits_applied,
        stage6_proposals=proposals,
        warnings=warnings,
        stats=stats,
    )

    OUT_GRAPH.parent.mkdir(parents=True, exist_ok=True)
    OUT_GRAPH.write_text(resolved.model_dump_json(indent=2), encoding="utf-8")
    OUT_LOG.write_text(log.model_dump_json(indent=2), encoding="utf-8")
    print(f"\nscritto {OUT_GRAPH}")
    print(f"scritto {OUT_LOG}")

    print(f"\nprovenances per nodo: mediana={median}, p95={p95}, max={stats.provenances_per_node_max}")
    print("top-5 nodi per numero di provenances:")
    for n in top:
        print(f"  {n.id:40s} type={n.type.value:10s} provenances={len(n.provenances)}")

    return resolved, log


if __name__ == "__main__":
    run()