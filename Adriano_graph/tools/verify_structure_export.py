"""Verifica export stage_4-4_structure: split snelli + round-trip lossless."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.deduplication_schema import ResolvedGraph, edge_key

RESOLVED = PROJECT_ROOT / "data" / "stage_4" / "3_resolve" / "resolved_graph.json"
STRUCTURE = PROJECT_ROOT / "data" / "stage_4" / "4_structure"
PROVENANCE_FIELDS = (
    "chunk_id",
    "model",
    "timestamp",
    "schema_version",
    "confidence",
    "evidence_span",
    "human_validated",
)


def main() -> None:
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8"))
    nodes = json.loads((STRUCTURE / "nodes.json").read_text(encoding="utf-8"))
    edges = json.loads((STRUCTURE / "edges.json").read_text(encoding="utf-8"))
    prov = json.loads((STRUCTURE / "provenance.json").read_text(encoding="utf-8"))

    errs: list[str] = []

    for n in nodes:
        if "provenances" in n:
            errs.append(f"node {n['id']} ha ancora provenances")
    for e in edges:
        if "provenances" in e:
            errs.append(
                f"edge {e['source_id']}->{e['target_id']} ha ancora provenances"
            )

    if prov.get("format_version") != "0.2.0":
        errs.append(f"format_version inatteso: {prov.get('format_version')!r}")
    if "nodes_provenances" not in prov or "edges_provenances" not in prov:
        errs.append("mancano nodes_provenances o edges_provenances")

    recon_nodes = []
    for n in nodes:
        row = dict(n)
        row["provenances"] = prov["nodes_provenances"].get(n["id"], [])
        recon_nodes.append(row)

    recon_edges = []
    for e in edges:
        row = dict(e)
        key = edge_key({
            "source_id": e["source_id"],
            "type": e["type"],
            "target_id": e["target_id"],
            "role": e.get("role"),
        })
        row["provenances"] = prov["edges_provenances"].get(key, [])
        recon_edges.append(row)

    orig = ResolvedGraph.model_validate(resolved)
    meta = {k: v for k, v in resolved.items() if k not in ("nodes", "edges")}
    recon = ResolvedGraph.model_validate({
        "nodes": recon_nodes,
        "edges": recon_edges,
        **meta,
    })

    if len(orig.nodes) != len(recon.nodes):
        errs.append(f"conteggio nodi: orig={len(orig.nodes)} recon={len(recon.nodes)}")
    if len(orig.edges) != len(recon.edges):
        errs.append(f"conteggio archi: orig={len(orig.edges)} recon={len(recon.edges)}")

    sort_node = lambda x: (x.type.value, x.id)
    sort_edge = lambda x: (x.source_id, x.type.value, x.target_id, x.role or "")

    for on, rn in zip(sorted(orig.nodes, key=sort_node), sorted(recon.nodes, key=sort_node)):
        if on.model_dump(mode="json") != rn.model_dump(mode="json"):
            errs.append(f"nodo diverso dopo round-trip: {on.id}")
            break

    for oe, re in zip(sorted(orig.edges, key=sort_edge), sorted(recon.edges, key=sort_edge)):
        if oe.model_dump(mode="json") != re.model_dump(mode="json"):
            errs.append(
                f"arco diverso dopo round-trip: {oe.source_id}->{oe.target_id} {oe.type.value}"
            )
            break

    sample = prov["nodes_provenances"]["adultita"][0]
    for field in PROVENANCE_FIELDS:
        if field not in sample:
            errs.append(f"provenance campione senza campo {field!r}")

    n_np = sum(len(v) for v in prov["nodes_provenances"].values())
    n_ep = sum(len(v) for v in prov["edges_provenances"].values())

    if errs:
        print("FAIL")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)

    print("OK round-trip lossless")
    print(f"  nodi={len(nodes)}  archi={len(edges)}")
    print(f"  provenance nodi={n_np}  provenance archi={n_ep}")
    print(f"  campi provenance: {', '.join(PROVENANCE_FIELDS)}")


if __name__ == "__main__":
    main()
