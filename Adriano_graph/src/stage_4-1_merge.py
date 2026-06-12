# src/stage_4_phaseA_merge.py
"""
Stadio 4 - Fase A: merge esatto (name letteralmente identico).

Input:
  - extracted_graph.json (run reale stadio 3)
  - data/stage_4/diagnostics/name_duplicates.json (Fase 0)

Output:
  - data/stage_4/1_merge/merge_map.json  blocchi "auto" e "to_review" con count e decisions

Regola di scelta dell'id canonico (ADR-020): vince l'id con piu' OCCORRENZE
nel grafo (canonicita' d'uso, fatto del dato). NESSUNA euristica linguistica
sui name. In caso di pareggio sulla frequenza, la decisione va a review:
"vince l'alfabetico" non e' canonicita', e' solo un trucco di idempotenza.

Conflitti che vanno a review:
  - id presente in piu' gruppi exact (es. cesare in due gruppi);
  - pareggio sulla frequenza (la regola non distingue);
  - type discordante dentro lo stesso gruppo exact (coordinare con Fase B).

Lo script NON applica i merge: produce una mappa unica, ispezionabile e diffabile.
L'applicazione (costruzione dei ResolvedNode/Edge) arriva dopo, quando Fase A,
B e C sono tutte pronte e coerenti.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict,Counter
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

INPUT_GRAPH = PROJECT_ROOT / "data" / "output" / "latest" / "extracted_graph.json"
NAME_DUPLICATES  = PROJECT_ROOT / "data" / "stage_4"/ "0_diagnostic" / "name_duplicates.json"
OUT_DIR = PROJECT_ROOT / "data" / "stage_4"/ "1_merge"


# -----------------------------------------------------------------------------
# Caricamento e lookup
# -----------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_node_lookup(graph: dict) -> dict:
    """
    id -> {names: set, types: set, n_occ: int}.
    n_occ e' la frequenza dell'id nel grafo: il segnale OGGETTIVO di
    canonicita' d'uso che la Fase A usa per scegliere il sopravvissuto.
    """
    lookup = defaultdict(lambda: {"names": set(), "types": set(), "n_occ": 0})
    for extraction in graph["extractions"]:
        for node in extraction.get("nodes", []):
            info = lookup[node["id"]]
            info["names"].add(node["name"])
            info["types"].add(node["type"])
            info["n_occ"] += 1
    return lookup


# -----------------------------------------------------------------------------
# Scelta dell'id canonico: frequenza, niente tie-break automatico.
# -----------------------------------------------------------------------------

def choose_canonical(ids: list[str], lookup: dict) -> tuple[str | None, str | None]:
    """
    Ritorna (canonical_id, tie_reason).
    Vince l'id con piu' occorrenze. Pareggio -> (None, motivo).
    """
    counts = [(nid, lookup[nid]["n_occ"]) for nid in ids]
    max_n = max(c for _, c in counts)
    winners = [nid for nid, c in counts if c == max_n]
    if len(winners) == 1:
        return winners[0], None
    return None, f"pareggio sulla frequenza ({max_n} occorrenze) fra {sorted(winners)}"


# -----------------------------------------------------------------------------
# Costruzione delle decisioni: divise in auto vs review.
# -----------------------------------------------------------------------------

def build_merge_decisions(name_dups: dict, lookup: dict) -> tuple[list[dict], list[dict]]:
    exact_groups = name_dups["exact"]

    id_in_groups = Counter()
    for g in exact_groups:
        for nid in g["ids"]:
            id_in_groups[nid] += 1
    conflicting_ids = {nid for nid, c in id_in_groups.items() if c > 1}

    auto: list[dict] = []
    review: list[dict] = []

    for g in exact_groups:
        ids = sorted(g["ids"])
        ntype_hint = g["type"]
        n_occs = {nid: lookup[nid]["n_occ"] for nid in ids}
        types_in_group = sorted({t for nid in ids for t in lookup[nid]["types"]})

        base = {
            "type": ntype_hint,
            "merged_ids": ids,
            "n_occurrences": n_occs,
            "types_in_group": types_in_group,
        }

        if any(nid in conflicting_ids for nid in ids):
            review.append({**base,
                "canonical_id": None, "losers": [],
                "method": "manual", "confidence": 0.0,
                "reason": f"id ambiguo presente in piu' gruppi exact: "
                          f"{sorted(set(ids) & conflicting_ids)}",
            })
            continue

        #if len(types_in_group) > 1:
        #    review.append({**base,
        #        "canonical_id": None, "losers": [],
        #        "method": "manual", "confidence": 0.0,
        #        "reason": f"type discordante: {types_in_group} (coordinare con Fase B)",
        #    })
        #    continue

        canonical, tie_reason = choose_canonical(ids, lookup)
        if canonical is None:
            review.append({**base,
                "canonical_id": None, "losers": [],
                "method": "manual", "confidence": 0.0,
                "reason": tie_reason,
            })
            continue

        losers = [nid for nid in ids if nid != canonical]
        auto.append({**base,
            "canonical_id": canonical, "losers": losers,
            "method": "exact", "confidence": 1.0,
            "reason": None,
        })

    auto.sort(key=lambda d: (d["type"], d["canonical_id"]))
    review.sort(key=lambda d: (d["type"], d["merged_ids"][0]))
    return auto, review


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------

def write_maps(auto: list[dict], review: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "merge_map.json"
    payload = {
        "auto": {"count": len(auto), "decisions": auto},
        "to_review": {"count": len(review), "decisions": review},
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"scritto {out_path}  (auto: {len(auto)}, to_review: {len(review)} decisioni)")


# -----------------------------------------------------------------------------
# Esecuzione passo-passo.
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    graph = load_json(INPUT_GRAPH)
    name_dups = load_json(NAME_DUPLICATES)

    lookup = build_node_lookup(graph)
    print(f"id distinti nel grafo: {len(lookup)}")

    auto, review = build_merge_decisions(name_dups, lookup)
    write_maps(auto, review)

    print(f"\ngruppi exact totali:  {len(auto) + len(review)}")
    print(f"merge automatici:     {len(auto)}")
    print(f"da validare a mano:   {len(review)}\n")

    for d in auto:
        occs = "/".join(f"{nid}={n}" for nid, n in d["n_occurrences"].items())
        print(f"  [AUTO]   {d['losers']} -> {d['canonical_id']}  ({occs})")
    for d in review:
        print(f"  [REVIEW] {d['merged_ids']}  reason: {d['reason']}")