# src/stage_4_phaseB_split.py
"""
Stadio 4 - Fase B: split deterministico delle collisioni di tipo.

Politica (ADR-020, revisione 2026-05-28):
  Per ogni id che compare con type discordante fra le occorrenze, riassegniamo
  a ciascuna occorrenza l'id <id_originario>__<type_lower>. Nessuna
  interpretazione, nessuna whitelist, nessun LLM. Le collisioni sono ~11 su 310
  chunk: noise residuo dell'estrazione, non un fenomeno semantico. Trattarle
  come tale e' overkill in questa fase del progetto.

Esempio concreto:
  id "roma" con 30 occorrenze Place e 1 occorrenza Person
    -> Place  -> "roma__place"   (un solo nodo finale, 30 provenances)
    -> Person -> "roma__person"  (un solo nodo finale, 1 provenance)

Input:  data/stage_4/diagnostics/type_collisions.json (Fase 0)
Output: data/stage_4/split_map.json

Lo script NON applica gli split al grafo: produce la mappa. L'applicazione
finale (costruzione dei ResolvedNode/Edge a partire da merge_map + split_map)
e' il passo successivo, separato.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))



TYPE_COLLISIONS  = PROJECT_ROOT / "data" / "stage_4"/ "0_diagnostic" / "type_collisions.json"
OUT_DIR = PROJECT_ROOT / "data" / "stage_4"/ "2_split"





def split_id(original_id: str, ntype: str) -> str:
    """Convenzione unica: <id>__<type_lower>. Deterministica, portabile, banale."""
    return f"{original_id}__{ntype.lower()}"


def build_split_map(type_collisions: dict) -> dict:
    """
    Per ogni id in collisione, costruisce la mappa di riassegnazione delle
    occorrenze e l'elenco dei nuovi id canonici prodotti dallo split.
    """
    decisions = []
    for nid, payload in type_collisions["collisions"].items():
        new_ids_by_type = {t: split_id(nid, t) for t in payload["types"]}
        # Anteprima per ispezione: quante occorrenze finiscono in ciascun nuovo id.
        occurrences_per_new_id = {new_id: 0 for new_id in new_ids_by_type.values()}
        for occ in payload["occurrences"]:
            occurrences_per_new_id[new_ids_by_type[occ["type"]]] += 1

        decisions.append({
            "original_id": nid,
            "types_observed": payload["types"],
            "new_ids_by_type": new_ids_by_type,
            "occurrences_per_new_id": occurrences_per_new_id,
        })

    decisions.sort(key=lambda d: d["original_id"])
    return {"decisions": decisions, "count": len(decisions)}


def write_split_map(split_map: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "split_map.json"
    out_path.write_text(
        json.dumps(split_map, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"scritto {out_path}")


if __name__ == "__main__":
    type_collisions = json.loads(TYPE_COLLISIONS.read_text(encoding="utf-8"))
    split_map = build_split_map(type_collisions)
    write_split_map(split_map)

    print(f"\ncollisioni splittate: {split_map['count']}")
    for d in split_map["decisions"]:
        print(f"  {d['original_id']:30s} -> {d['new_ids_by_type']}  "
              f"({d['occurrences_per_new_id']})")