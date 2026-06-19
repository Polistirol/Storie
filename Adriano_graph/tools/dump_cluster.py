# src/dump_cluster.py
"""
Helper d'ispezione per il 5-3 (non è uno stadio della pipeline).

Dato un (o più) cluster_id prodotti dal 5-3a, stampa i membri del cluster con
name + description + freq, pescando le description dal grafo. Serve a preparare
i dati per la prova del giudizio gerarchico su Qwen, senza copiare nulla a mano.

Default: stampa i cluster #1 (lutto/memoria, conflato) e #7 (corpo, col
conflitto anima/corpo), i due casi-stress concordati. Override con --clusters.

Input (default: data/stage_5/3_hierarchy/hierarchy_candidates.json
                + data/stage_5/2_themes/enriched_graph.json)

Uso:
  python -m tools.dump_cluster                 # cluster 1 e 7
  python -m tools.dump_cluster --clusters 0 5  # altri cluster
  python -m tools.dump_cluster --json          # output JSON (per darlo in pasto a uno script)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
DEFAULT_CANDIDATES = _PROJECT_ROOT / "data" / "stage_5" / "3_hierarchy" / "hierarchy_candidates.json"
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "2_themes" / "enriched_graph.json"


def _load(candidates: Path, graph: Path):
    cand = json.loads(candidates.read_text(encoding="utf-8"))
    g = json.loads(graph.read_text(encoding="utf-8"))
    desc_by_id = {n["id"]: n.get("description") for n in g.get("nodes", []) if n.get("type") == "Theme"}
    clusters_by_id = {c["cluster_id"]: c for c in cand.get("clusters", [])}
    return cand, clusters_by_id, desc_by_id


def build(candidates: Path, graph: Path, cluster_ids: list[int]) -> list[dict]:
    cand, clusters_by_id, desc_by_id = _load(candidates, graph)
    out = []
    for cid in cluster_ids:
        c = clusters_by_id.get(cid)
        if c is None:
            out.append({"cluster_id": cid, "error": "cluster_id non trovato"})
            continue
        members = []
        for msmall in c["members"]:
            mid = msmall["id"]
            members.append({
                "id": mid,
                "name": msmall["name"],
                "freq": msmall["freq"],
                "description": desc_by_id.get(mid),
            })
        out.append({
            "cluster_id": cid,
            "size": c["size"],
            "cohesion": c["cohesion"],
            "existing_caps": c.get("existing_caps", []),
            "refinement_nodes": c.get("refinement_nodes", []),
            "members": members,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Stampa i membri di uno o più cluster con name+description.")
    ap.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    ap.add_argument("--clusters", type=int, nargs="+", default=[1, 7], help="cluster_id da stampare (default 1 7)")
    ap.add_argument("--json", action="store_true", help="output JSON invece del formato leggibile")
    args = ap.parse_args()

    data = build(args.candidates, args.graph, args.clusters)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    for c in data:
        if "error" in c:
            print(f"\n=== cluster #{c['cluster_id']}: {c['error']} ===")
            continue
        caps = f"  cap_esistenti={c['existing_caps']}" if c["existing_caps"] else ""
        ref = f"  refinement={c['refinement_nodes']}" if c["refinement_nodes"] else ""
        print(f"\n=== cluster #{c['cluster_id']}  (size {c['size']}, cohesion {c['cohesion']}){caps}{ref} ===")
        for m in c["members"]:
            desc = (m["description"] or "(nessuna descrizione)").replace("\n", " ")
            print(f"  - id: {m['id']}  | name: {m['name']}  | freq: {m['freq']}")
            print(f"      desc: {desc}")


if __name__ == "__main__":
    main()