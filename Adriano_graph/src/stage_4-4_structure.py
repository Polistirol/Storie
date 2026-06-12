# src/stage_4-4_structure.py
"""
Stadio 4 — Fase D (structure): viste piatte per ispezione e visualizzazione.

Legge `resolved_graph.json` (fonte di verità da stage 4-3), valida `ResolvedGraph`
e scrive tre file in `data/stage_4/4_structure/`:

  - nodes.json        nodi senza provenances
  - edges.json        archi senza provenances
  - provenance.json   provenance estratte, metadati completi, partizionate per entità

Formato `provenance.json` (v0.2.0):
  {
    "format_version": "0.2.0",
    "nodes_provenances": { "<node_id>": [ Provenance, ... ], ... },
    "edges_provenances": { "<source|type|target[|role]>": [ Provenance, ... ], ... }
  }

Ricostruzione (lossless rispetto a resolved_graph per nodi/archi/provenances):
  node["provenances"] = provenance["nodes_provenances"].get(node["id"], [])
  edge["provenances"] = provenance["edges_provenances"].get(edge_key(edge), [])

Vedi ADR-021.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.deduplication_schema import ResolvedGraph, edge_key

INPUT_GRAPH = PROJECT_ROOT / "data" / "stage_4" / "3_resolve" / "resolved_graph.json"
OUT_DIR = PROJECT_ROOT / "data" / "stage_4" / "4_structure"

STAGE_VERSION = "0.2.0"
PROVENANCE_FORMAT_VERSION = "0.2.0"


def _edge_lookup_key(source_id: str, edge_type: str, target_id: str, role: str | None) -> str:
    return edge_key({
        "source_id": source_id,
        "type": edge_type,
        "target_id": target_id,
        "role": role,
    })


def build_structure_views(
    graph: ResolvedGraph,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Proietta il grafo risolto in nodi, archi e provenance partizionate."""
    nodes_out: list[dict[str, Any]] = []
    nodes_provenances: dict[str, list[dict[str, Any]]] = {}
    for n in graph.nodes:
        row = n.model_dump(mode="json")
        nodes_provenances[n.id] = row.pop("provenances", [])
        nodes_out.append(row)

    edges_out: list[dict[str, Any]] = []
    edges_provenances: dict[str, list[dict[str, Any]]] = {}
    for e in graph.edges:
        row = e.model_dump(mode="json")
        key = _edge_lookup_key(e.source_id, e.type.value, e.target_id, e.role)
        edges_provenances[key] = row.pop("provenances", [])
        edges_out.append(row)

    provenance_out = {
        "format_version": PROVENANCE_FORMAT_VERSION,
        "nodes_provenances": nodes_provenances,
        "edges_provenances": edges_provenances,
    }
    return nodes_out, edges_out, provenance_out


def run(
    input_path: Path = INPUT_GRAPH,
    out_dir: Path = OUT_DIR,
) -> ResolvedGraph:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    graph = ResolvedGraph.model_validate(raw)

    nodes_out, edges_out, provenance_out = build_structure_views(graph)

    n_node_prov = sum(len(v) for v in provenance_out["nodes_provenances"].values())
    n_edge_prov = sum(len(v) for v in provenance_out["edges_provenances"].values())

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nodes.json").write_text(
        json.dumps(nodes_out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "edges.json").write_text(
        json.dumps(edges_out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "provenance.json").write_text(
        json.dumps(provenance_out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"input:  {input_path}")
    print(f"output: {out_dir}/")
    print(f"  nodi:              {len(nodes_out)}")
    print(f"  archi:             {len(edges_out)}")
    print(f"  provenance nodi:   {n_node_prov}")
    print(f"  provenance archi:  {n_edge_prov}")
    return graph


if __name__ == "__main__":
    run()
