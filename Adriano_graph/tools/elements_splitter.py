import json
from pathlib import Path
from collections import defaultdict
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

src = PROJECT_ROOT / "data" / "stage_4" / "3_resolve" / "resolved_graph.json"
out = PROJECT_ROOT / "data" / "output" / "splitted"
#src = Path(r".\resolved_graph.json")

with open(src, encoding="utf-8") as f:
    graph = json.load(f)

#out = src.parent

# nodi (senza provenances)
nodes = []
for n in graph["nodes"]:
    node = {k: v for k, v in n.items() if k != "provenances"}
    nodes.append(node)

# provenance (flat, con parent_id)
provenance = []
for n in graph["nodes"]:
    for i, prov in enumerate(n.get("provenances", [])):
        provenance.append({
            "prov_id":       f"{n['id']}__{i}",
            "parent_id":     n["id"],
            "chunk_id":      prov["chunk_id"],
            "confidence":    prov.get("confidence"),
            "evidence_span": prov.get("evidence_span"),
        })

# archi
edges = graph["edges"]

out.mkdir(parents=True, exist_ok=True)

with open(out / "nodes.json",      "w", encoding="utf-8") as f:
    json.dump(nodes,      f, ensure_ascii=False, indent=2)
with open(out / "edges.json",      "w", encoding="utf-8") as f:
    json.dump(edges,      f, ensure_ascii=False, indent=2)
with open(out / "provenance.json", "w", encoding="utf-8") as f:
    json.dump(provenance, f, ensure_ascii=False, indent=2)

print(f"nodi:       {len(nodes)}")
print(f"archi:      {len(edges)}")
print(f"provenance: {len(provenance)}")