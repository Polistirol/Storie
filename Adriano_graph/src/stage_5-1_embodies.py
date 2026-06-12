# src/stage_5-1_embodies.py
"""
Stadio 5-1 — Materializzazione deterministica degli archi EMBODIES.

Primo sotto-stadio dell'enrich (stadio 5). NIENTE LLM, NIENTE embeddings:
prende le `Stage6Proposal` di tipo EMBODIES prodotte dallo stadio 4 (split di
id ambigui Event/Theme e Phase/Theme) e le trasforma in archi `EMBODIES` reali
sul grafo deduplicato.

Formato
-------
Spina dorsale dell'enrich = `resolved_graph.json` (shape `ResolvedGraph`): nodi
e archi, ciascuno con le `provenances` INLINE e complete di metadati. È lo stesso
formato che consuma lo stadio 3.5 (Neo4j). I file flat di `4_structure/` sono
solo export esplorativi, derivati e rigenerabili: NON sono fonte di verità e non
vengono toccati qui.

Input  (default: data/stage_4/3_resolve/)
-----
- resolved_graph.json  (`ResolvedGraph`): nodi + archi canonici.
- resolver_log.json    (`Stage4Log`): contiene `stage6_proposals`. Le proposte
  vivono nel LOG, non nel grafo.

Output (default: data/stage_5/1_embodies/)
------
- enriched_graph.json   (`ResolvedGraph`: resolved + nuovi archi EMBODIES)
- embodies_map.json      (decisioni ispezionabili, una per proposta)

Idempotenza
-----------
5-1 è funzione pura degli artefatti di stadio 4: rilanciarlo rilegge
`resolved_graph.json` (mai il proprio output) e riproduce lo stesso risultato.
Gli archi EMBODIES già presenti nel grafo vengono riconosciuti e NON duplicati
(dedup per la chiave (source_id, type, target_id, role)).

Compatibilità
-------------
`EDGE_COMPATIBILITY` ammette EMBODIES solo `(Event|Person) -> Theme`. Una
proposta Phase/Theme produrrebbe un `Phase -> Theme` strutturalmente invalido:
viene LOGGATA e SALTATA, non materializzata (scelta conservativa, ADR-023 (a)).
Lo stesso vincolo è ri-enforced da `ResolvedGraph._referential_integrity` alla
costruzione del grafo finale: la validazione è la rete di sicurezza.

Provenienza degli archi sintetici (ADR-023, da redigere)
--------------------------------------------------------
Un EMBODIES da split non viene da un singolo chunk. Per restare tracciabili:
- chunk_id: un chunk REALE che àncora l'arco. Si preferisce un chunk presente
  sia nelle provenances del nodo Event sia in quelle del Theme (co-occorrenza =
  evidenza più forte); in mancanza, il primo chunk (ordinato) del nodo Event, e
  l'arco viene marcato `review_needed=True`.
- model: sentinella "stage_5-1_embodies" (NON un modello LLM: è derivazione
  deterministica da una decisione di stadio 4).
- confidence / evidence_span: dalla proposta (`confidence` e `note`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.schema import EdgeType, Provenance, SCHEMA_VERSION, is_edge_valid  # noqa: E402
from src.deduplication_schema import (  # noqa: E402
    DEDUP_SCHEMA_VERSION,
    ResolvedEdge,
    ResolvedGraph,
    ResolvedNode,
    Stage6Proposal,
    align_edge_endpoint_types,
)

STAGE_VERSION = "0.1.0"
ENRICH_PROVENANCE_MODEL = "stage_5-1_embodies"

# -----------------------------------------------------------------------------
# Path di default (module-relative, indipendenti dal cwd). Sovrascrivibili via CLI.
# Verifica che combacino col tuo layout: in particolare il nome del file di log
# (resolver_log.json vs stage_4_log.json).
# -----------------------------------------------------------------------------
_STAGE_4_RESOLVE = _PROJECT_ROOT / "data" / "stage_4" / "3_resolve"
DEFAULT_RESOLVED = _STAGE_4_RESOLVE / "resolved_graph.json"
DEFAULT_LOG = _STAGE_4_RESOLVE / "resolver_log.json"
DEFAULT_OUT_DIR = _PROJECT_ROOT / "data" / "stage_5" / "1_embodies"


# -----------------------------------------------------------------------------
# Modelli di decisione / mappa ispezionabile (inline: promuovibili a uno schema
# stage_5 condiviso quando arrivano 5-2/5-3).
# -----------------------------------------------------------------------------

class EmbodiesDecision(BaseModel):
    kind: str
    source_id: str
    target_id: str
    action: str  # materialized | skipped_incompatible | skipped_duplicate | skipped_dangling | skipped_not_embodies
    reason: Optional[str] = None
    source_type: Optional[str] = None
    target_type: Optional[str] = None
    grounding_chunk_id: Optional[str] = None
    co_occurrence: Optional[bool] = None


class EmbodiesMap(BaseModel):
    source_resolved_graph: str
    source_log: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    dedup_schema_version: str = DEDUP_SCHEMA_VERSION
    proposals_total: int
    materialized: int
    skipped_incompatible: int
    skipped_duplicate: int
    skipped_dangling: int
    skipped_not_embodies: int
    decisions: list[EmbodiesDecision] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------------

def load_resolved_graph(path: Path) -> ResolvedGraph:
    with path.open("r", encoding="utf-8") as f:
        return ResolvedGraph(**json.load(f))


def load_proposals(path: Path) -> list[Stage6Proposal]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("stage6_proposals", [])
    return [Stage6Proposal(**p) for p in raw]


def pick_grounding_chunk(src: ResolvedNode, tgt: ResolvedNode) -> tuple[str, bool]:
    """
    Sceglie il chunk che àncora l'arco. Preferisce un chunk in cui compaiono
    sia il nodo sorgente sia il nodo destinazione (co-occorrenza). Altrimenti
    ripiega sul primo chunk (ordinato) del nodo sorgente.
    Ritorna (chunk_id, co_occurrence).
    """
    src_chunks = sorted({p.chunk_id for p in src.provenances})
    tgt_chunks = {p.chunk_id for p in tgt.provenances}
    common = [c for c in src_chunks if c in tgt_chunks]
    if common:
        return common[0], True
    if src_chunks:
        return src_chunks[0], False
    # Caso limite: nodo senza provenances (non dovrebbe accadere, il validator
    # di ResolvedNode lo vieta). Fallback sul target per non esplodere.
    tgt_only = sorted(tgt_chunks)
    return (tgt_only[0] if tgt_only else "UNKNOWN"), False


def build_embodies_edge(
    prop: Stage6Proposal,
    src: ResolvedNode,
    tgt: ResolvedNode,
    now: datetime,
) -> tuple[ResolvedEdge, str, bool]:
    chunk_id, co_occ = pick_grounding_chunk(src, tgt)
    prov = Provenance(
        chunk_id=chunk_id,
        model=ENRICH_PROVENANCE_MODEL,
        timestamp=now,
        schema_version=SCHEMA_VERSION,
        confidence=prop.confidence,
        evidence_span=(prop.note or "")[:200] or None,
        human_validated=False,
    )
    edge = ResolvedEdge(
        source_id=prop.source_id,
        target_id=prop.target_id,
        type=EdgeType.EMBODIES,
        source_type=src.type,
        target_type=tgt.type,
        description=prop.note,
        role=None,
        provenances=[prov],
        merged_from=[f"stage5_embodies:{prop.source_id}|EMBODIES|{prop.target_id}"],
        merge_confidence=prop.confidence,
        review_needed=True,
        review_reason=(
            f"EMBODIES da split di id ambiguo (proposta stadio 4, "
            f"confidence {prop.confidence}); grounding chunk {chunk_id}, "
            f"co_occorrenza={co_occ}"
        ),
    )
    return edge, chunk_id, co_occ


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(resolved_path: Path, log_path: Path, out_dir: Path, dry_run: bool = False) -> EmbodiesMap:
    graph = load_resolved_graph(resolved_path)
    proposals = load_proposals(log_path)

    node_index: dict[str, ResolvedNode] = {n.id: n for n in graph.nodes}
    existing_keys = {(e.source_id, e.type, e.target_id, e.role) for e in graph.edges}

    now = datetime.now(timezone.utc)
    decisions: list[EmbodiesDecision] = []
    new_edges: list[ResolvedEdge] = []

    for prop in proposals:
        base = dict(kind=prop.kind, source_id=prop.source_id, target_id=prop.target_id)

        if not prop.kind.startswith("embodies"):
            decisions.append(EmbodiesDecision(**base, action="skipped_not_embodies",
                                              reason=f"kind non EMBODIES: {prop.kind!r}"))
            continue

        src = node_index.get(prop.source_id)
        tgt = node_index.get(prop.target_id)
        if src is None or tgt is None:
            missing = [i for i, n in [(prop.source_id, src), (prop.target_id, tgt)] if n is None]
            decisions.append(EmbodiesDecision(**base, action="skipped_dangling",
                                              reason=f"nodi inesistenti nel grafo: {missing}"))
            continue

        if not is_edge_valid(EdgeType.EMBODIES, src.type, tgt.type):
            decisions.append(EmbodiesDecision(
                **base, action="skipped_incompatible",
                source_type=src.type.value, target_type=tgt.type.value,
                reason=f"{src.type.value} -[EMBODIES]-> {tgt.type.value} non ammesso da EDGE_COMPATIBILITY",
            ))
            continue

        key = (prop.source_id, EdgeType.EMBODIES, prop.target_id, None)
        if key in existing_keys:
            decisions.append(EmbodiesDecision(
                **base, action="skipped_duplicate",
                source_type=src.type.value, target_type=tgt.type.value,
                reason="arco EMBODIES già presente nel grafo",
            ))
            continue

        edge, chunk_id, co_occ = build_embodies_edge(prop, src, tgt, now)
        new_edges.append(edge)
        existing_keys.add(key)
        decisions.append(EmbodiesDecision(
            **base, action="materialized",
            source_type=src.type.value, target_type=tgt.type.value,
            grounding_chunk_id=chunk_id, co_occurrence=co_occ,
        ))

    # Costruzione del grafo arricchito. La validazione (referential integrity +
    # EDGE_COMPATIBILITY) gira QUI in __init__: se un EMBODIES fosse invalido,
    # solleva. align_* è una ri-derivazione difensiva dei tipi degli estremi.
    enriched = ResolvedGraph(
        nodes=graph.nodes,
        edges=graph.edges + new_edges,
        source_run=graph.source_run,
        source_schema_version=graph.source_schema_version,
        source_prompt_version=graph.source_prompt_version,
        dedup_schema_version=DEDUP_SCHEMA_VERSION,
        stage_version=graph.stage_version,  # identità stage-5 tracciata in embodies_map + filename
        timestamp=now,
    )
    enriched = align_edge_endpoint_types(enriched)

    counts = {a: sum(1 for d in decisions if d.action == a) for a in
              ("materialized", "skipped_incompatible", "skipped_duplicate",
               "skipped_dangling", "skipped_not_embodies")}
    emap = EmbodiesMap(
        source_resolved_graph=str(resolved_path),
        source_log=str(log_path),
        timestamp=now,
        proposals_total=len(proposals),
        materialized=counts["materialized"],
        skipped_incompatible=counts["skipped_incompatible"],
        skipped_duplicate=counts["skipped_duplicate"],
        skipped_dangling=counts["skipped_dangling"],
        skipped_not_embodies=counts["skipped_not_embodies"],
        decisions=decisions,
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "enriched_graph.json").write_text(
            enriched.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / "embodies_map.json").write_text(
            emap.model_dump_json(indent=2), encoding="utf-8")

    return emap


def _print_summary(emap: EmbodiesMap, dry_run: bool) -> None:
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"{tag}stage 5-1 — EMBODIES")
    print(f"  proposte totali        : {emap.proposals_total}")
    print(f"  materializzate         : {emap.materialized}")
    print(f"  saltate (incompatibili): {emap.skipped_incompatible}")
    print(f"  saltate (duplicate)    : {emap.skipped_duplicate}")
    print(f"  saltate (dangling)     : {emap.skipped_dangling}")
    print(f"  saltate (non-embodies) : {emap.skipped_not_embodies}")
    for d in emap.decisions:
        line = f"   - {d.action:22s} {d.source_id} -> {d.target_id}"
        if d.action == "materialized":
            line += f"  (chunk {d.grounding_chunk_id}, co_occ={d.co_occurrence})"
        elif d.reason:
            line += f"  [{d.reason}]"
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-1: materializza gli archi EMBODIES dalle proposte di stadio 4.")
    ap.add_argument("--resolved", type=Path, default=DEFAULT_RESOLVED, help="path di resolved_graph.json")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="path del log di stadio 4 (contiene stage6_proposals)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="cartella di output")
    ap.add_argument("--dry-run", action="store_true", help="non scrive file, stampa solo il riepilogo")
    args = ap.parse_args()

    emap = run(args.resolved, args.log, args.out, dry_run=args.dry_run)
    _print_summary(emap, args.dry_run)


if __name__ == "__main__":
    main()