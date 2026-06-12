# src/stage_4_phase0_diagnostics.py
"""
Stadio 4 - Fase 0: artefatti diagnostici leggeri.

Legge l'extracted_graph.json dello stadio 3 e produce file piccoli e mirati,
uno per tipo di problema di resolution. NON modifica il grafo: sola lettura.

Pensato per essere eseguito passo per passo in debug:
ogni step e' una funzione pura che prende i dati gia' caricati e ritorna
(o scrive) il suo risultato. Lanciare le funzioni una alla volta dal blocco
in fondo, o riga per riga nel debugger.

Output in data/stage_4/diagnostics/:
  - nodes_index.json       (0.2) lista scarna + conteggi
  - type_collisions.json   (0.3) id con type discordante
  - name_duplicates.json   (0.4) gruppi da fondere per nome (exact / near)
  - themes_dump.json       (0.5) Theme con id/name/description (foto PRE-merge)
"""

from __future__ import annotations

import json
import sys
import re
from collections import Counter, defaultdict
from pathlib import Path

# -----------------------------------------------------------------------------
# Costanti. Cambiare STAGE_3_GRAPH_PATH al run reale (l'ultimo full_run a 0.4.2).
# -----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STAGE_3_GRAPH_PATH = PROJECT_ROOT / "data" / "output" / "latest" / "extracted_graph.json"
STAGE_4_DIR = PROJECT_ROOT / "data" / "stage_4"/ "0_diagnostic"


#STAGE_3_GRAPH_PATH = Path("data/stage_3/full_runs/REPLACE_WITH_DATETIME/extracted_graph.json")
#STAGE_4_DIR = Path("data/stage_4/diagnostics")

DESC_TRUNCATE = 200  # troncamento description nel themes_dump (0.5)


# -----------------------------------------------------------------------------
# Helper condivisi
# -----------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """
    Normalizzazione BLANDA del name, condivisa fra 0.2, 0.4 e (poi) Fase A.
    strip + casefold + collasso spazi. NON tocca accenti ne' punteggiatura:
    "citta'" != "citta", "L'amore" != "lamore".
    """
    return re.sub(r"\s+", " ", name.strip()).casefold()


def load_graph(path: Path = STAGE_3_GRAPH_PATH) -> dict:
    """Carica il JSON grezzo (niente Pydantic in Fase 0: vogliamo scoprire i malformati, non esplodere)."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def iter_nodes(graph: dict):
    """Genera (chunk_id, node_dict) su tutti i nodi di tutti i chunk."""
    for extraction in graph["extractions"]:
        cid = extraction["chunk_id"]
        for node in extraction.get("nodes", []):
            yield cid, node


def iter_edges(graph: dict):
    """Genera (chunk_id, edge_dict) su tutti gli archi di tutti i chunk."""
    for extraction in graph["extractions"]:
        cid = extraction["chunk_id"]
        for edge in extraction.get("edges", []):
            yield cid, edge


def write_json(obj: dict, filename: str) -> None:
    """Scrive ordinato e indentato in STAGE_4_DIR. sort_keys per idempotenza/diff puliti."""
    STAGE_4_DIR.mkdir(parents=True, exist_ok=True)
    out_path = STAGE_4_DIR / filename
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"scritto {out_path}  ({out_path.stat().st_size} bytes)")


# -----------------------------------------------------------------------------
# 0.2 - nodes_index.json: versione scarna di tutti i nodi + conteggi.
# -----------------------------------------------------------------------------

def build_nodes_index(graph: dict) -> dict:
    nodes = []
    id_counter = Counter()
    type_name_counter = Counter()

    for _cid, node in iter_nodes(graph):
        nid = node["id"]
        ntype = node["type"]
        nname = node["name"]
        nodes.append({
            "id": nid,
            "type": ntype,
            "name": nname,
            "aliases": node.get("aliases", []),
        })
        id_counter[nid] += 1
        type_name_counter[(ntype, normalize_name(nname))] += 1

    # Counter con chiave tupla non e' serializzabile in JSON: appiattisco in stringa "type||name".
    type_name_counts = {f"{t}||{n}": c for (t, n), c in type_name_counter.items()}

    nodes.sort(key=lambda x: (x["type"], x["id"]))
    return {
        "nodes": nodes,
        "id_counts": dict(id_counter),
        "type_name_counts": type_name_counts,
    }


# -----------------------------------------------------------------------------
# 0.3 - type_collisions.json: id che cambiano type tra le occorrenze.
# -----------------------------------------------------------------------------

def build_type_collisions(graph: dict) -> dict:
    by_id = defaultdict(list)
    for _cid, node in iter_nodes(graph):
        prov = node.get("provenance", {})
        by_id[node["id"]].append({
            "chunk_id": prov.get("chunk_id"),
            "type": node["type"],
            "name": node["name"],
            "evidence_span": prov.get("evidence_span"),
        })

    collisions = {}
    for nid, occurrences in by_id.items():
        distinct_types = {occ["type"] for occ in occurrences}
        if len(distinct_types) > 1:
            collisions[nid] = {
                "types": sorted(distinct_types),
                "occurrences": sorted(occurrences, key=lambda o: (o["type"], o["chunk_id"] or "")),
            }

    return {"collisions": collisions, "count": len(collisions)}


# -----------------------------------------------------------------------------
# 0.4 - name_duplicates.json: gruppi da fondere per nome (exact / near).
# -----------------------------------------------------------------------------

def build_name_duplicates(graph: dict) -> dict:
    # Raccolgo per (type, name_normalizzato) gli id distinti e le forme originali.
    groups = defaultdict(lambda: {"ids": set(), "names": set(), "aliases": set()})
    for _cid, node in iter_nodes(graph):
        key = (node["type"], normalize_name(node["name"]))
        g = groups[key]
        g["ids"].add(node["id"])
        g["names"].add(node["name"])
        for a in node.get("aliases", []):
            g["aliases"].add(a)

    # EXACT: stesso (type, name_normalizzato) con piu' di un id distinto.
    exact = []
    for (ntype, nname), g in groups.items():
        if len(g["ids"]) > 1:
            exact.append({
                "type": ntype,
                "name_normalized": nname,
                "ids": sorted(g["ids"]),
                "names": sorted(g["names"]),
            })
    exact.sort(key=lambda x: (x["type"], x["name_normalized"]))

    # NEAR: coppie di gruppi DIVERSI, stesso type, dove un name e' prefisso
    # dell'altro oppure compare negli alias dell'altro. Volutamente generoso:
    # meglio una coppia in piu' da confermare a mano che perderne una.
    near = []
    keys = list(groups.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            t1, n1 = keys[i]
            t2, n2 = keys[j]
            if t1 != t2:
                continue
            g1, g2 = groups[keys[i]], groups[keys[j]]
            prefix_hit = n1.startswith(n2) or n2.startswith(n1)
            alias_hit = bool(
                {normalize_name(a) for a in g1["aliases"]} & {n2}
                or {normalize_name(a) for a in g2["aliases"]} & {n1}
            )
            if prefix_hit or alias_hit:
                near.append({
                    "type": t1,
                    "name_a": n1,
                    "name_b": n2,
                    "ids_a": sorted(g1["ids"]),
                    "ids_b": sorted(g2["ids"]),
                    "reason": "prefix" if prefix_hit else "alias",
                })
    near.sort(key=lambda x: (x["type"], x["name_a"], x["name_b"]))

    return {"exact": exact, "near": near, "exact_count": len(exact), "near_count": len(near)}


# -----------------------------------------------------------------------------
# 0.5 - themes_dump.json: Theme con id/name/description. FOTO PRE-MERGE
# (i duplicati Theme ci sono ancora tutti: la Fase A non e' ancora girata).
# -----------------------------------------------------------------------------

def build_themes_dump(graph: dict) -> dict:
    themes = []
    for _cid, node in iter_nodes(graph):
        if node["type"] != "Theme":
            continue
        desc = node.get("description") or ""
        if len(desc) > DESC_TRUNCATE:
            desc = desc[:DESC_TRUNCATE] + "..."
        themes.append({
            "id": node["id"],
            "name": node["name"],
            "description": desc,
        })
    themes.sort(key=lambda x: x["name"].casefold())
    return {"themes": themes, "count": len(themes)}


# -----------------------------------------------------------------------------
# Esecuzione passo-passo. Commenta/scommenta o lancia in debug una alla volta.
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    graph = load_graph()
    print(f"chunk: {len(graph['extractions'])}")
    print(f"nodi totali: {sum(1 for _ in iter_nodes(graph))}")
    print(f"archi totali: {sum(1 for _ in iter_edges(graph))}")

    # 0.2
    nodes_index = build_nodes_index(graph)
    write_json(nodes_index, "nodes_index.json")

    # 0.3
    type_collisions = build_type_collisions(graph)
    write_json(type_collisions, "type_collisions.json")
    print(f"id in collisione di tipo: {type_collisions['count']}")

    # 0.4
    name_duplicates = build_name_duplicates(graph)
    write_json(name_duplicates, "name_duplicates.json")
    print(f"gruppi exact: {name_duplicates['exact_count']}  coppie near: {name_duplicates['near_count']}")

    # 0.5
    themes_dump = build_themes_dump(graph)
    write_json(themes_dump, "themes_dump.json")
    print(f"theme (pre-merge): {themes_dump['count']}")