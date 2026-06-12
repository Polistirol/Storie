# src/deduplication_schema.py
"""
Schema post-resolution dello stadio 4.

Responsabilità: i modelli del grafo DOPO il merge/split. Distinti da `schema.py`
(che resta lo schema chunk-locale dell'estrattore, SCHEMA_VERSION) perché i campi
qui — `merged_from`, `merge_method`, `review_needed`, lista di `provenances` — esistono
SOLO in conseguenza della resolution.

Cosa NON fa questo modulo:
- non estrae (è lo stadio 3);
- non scrive su Neo4j (è lo stadio 3.5, sull'output deduplicato);
- non arricchisce con relazioni nuove cross-chunk (è lo stadio 6).

Riusa `Provenance`, `NodeType`, `EdgeType`, `INVOLVES_ROLES` da `schema.py`: niente duplicazione.

Il `ResolvedGraph` contiene SOLO nodi e archi: è il deliverable del grafo.
Le proposte per lo stadio 6 e gli altri artefatti di processo (log, statistiche,
warning) vivono in `Stage4Log`, file separato.

Vedi ADR-020 per la strategia di resolution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from src.schema import (
    EdgeType,
    INVOLVES_ROLES,
    NodeType,
    Provenance,
    SCHEMA_VERSION,
    is_edge_valid,
)

DEDUP_SCHEMA_VERSION = "0.1.1"


# -----------------------------------------------------------------------------
# Metodi di merge ammessi.
# -----------------------------------------------------------------------------

MERGE_METHODS: tuple[str, ...] = (
    "none",
    "exact",
    "near_alias",
    "type_reconcile",
    "split",
    "synonym_llm",
    "manual",
)


# -----------------------------------------------------------------------------
# ResolvedNode
# -----------------------------------------------------------------------------

class ResolvedNode(BaseModel):
    id: str
    type: NodeType
    name: str
    description: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    provenances: list[Provenance] = Field(default_factory=list)

    merged_from: list[str] = Field(default_factory=list)
    merge_method: str = "none"
    merge_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    review_needed: bool = False
    review_reason: Optional[str] = None

    @model_validator(mode="after")
    def _coherence(self) -> "ResolvedNode":
        if not self.provenances:
            raise ValueError(f"ResolvedNode {self.id!r}: provenances vuota")
        if self.merge_method not in MERGE_METHODS:
            raise ValueError(f"merge_method {self.merge_method!r} non ammesso; valori: {MERGE_METHODS}")
        if self.merge_method == "none" and len(self.merged_from) > 1:
            raise ValueError(f"ResolvedNode {self.id!r}: merge_method 'none' ma merged_from ha {len(self.merged_from)} elementi")
        if self.review_needed and not self.review_reason:
            raise ValueError(f"ResolvedNode {self.id!r}: review_needed=True ma review_reason mancante")
        return self

    @classmethod
    def from_single(cls, node: dict) -> "ResolvedNode":
        return cls(
            id=node["id"],
            type=node["type"],
            name=node["name"],
            description=node.get("description"),
            aliases=list(node.get("aliases", [])),
            provenances=[Provenance(**node["provenance"])],
            merged_from=[node["id"]],
            merge_method="none",
            merge_confidence=1.0,
        )


# -----------------------------------------------------------------------------
# ResolvedEdge
# -----------------------------------------------------------------------------

class ResolvedEdge(BaseModel):
    source_id: str
    target_id: str
    type: EdgeType
    source_type: NodeType
    target_type: NodeType
    description: Optional[str] = None
    role: Optional[str] = None
    provenances: list[Provenance] = Field(default_factory=list)

    merged_from: list[str] = Field(default_factory=list)
    merge_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    review_needed: bool = False
    review_reason: Optional[str] = None

    @model_validator(mode="after")
    def _coherence(self) -> "ResolvedEdge":
        if not self.provenances:
            raise ValueError(f"ResolvedEdge {self.source_id}->{self.target_id}: provenances vuota")
        if self.type == EdgeType.INVOLVES:
            if self.role is None:
                raise ValueError("role obbligatorio per INVOLVES")
            if self.role not in INVOLVES_ROLES:
                raise ValueError(f"role {self.role!r} non ammesso per INVOLVES; valori: {INVOLVES_ROLES}")
        else:
            if self.role is not None:
                raise ValueError(f"role deve essere None per {self.type.value!r}, ricevuto {self.role!r}")
        if self.review_needed and not self.review_reason:
            raise ValueError("review_needed=True ma review_reason mancante")
        return self

    @classmethod
    def from_single(
        cls,
        edge: dict,
        type_by_id: dict[str, NodeType],
    ) -> "ResolvedEdge":
        src = edge["source_id"]
        tgt = edge["target_id"]
        return cls(
            source_id=src,
            target_id=tgt,
            type=edge["type"],
            source_type=type_by_id[src],
            target_type=type_by_id[tgt],
            description=edge.get("description"),
            role=edge.get("role"),
            provenances=[Provenance(**edge["provenance"])],
            merged_from=[edge_key(edge)],
            merge_confidence=1.0,
        )


def edge_key(edge: dict) -> str:
    """Chiave identificativa di un arco grezzo. Include role per gli INVOLVES."""
    role = edge.get("role")
    base = f"{edge['source_id']}|{edge['type']}|{edge['target_id']}"
    return f"{base}|{role}" if role else base


# -----------------------------------------------------------------------------
# ResolvedGraph — il deliverable. SOLO nodi e archi.
# -----------------------------------------------------------------------------

class ResolvedGraph(BaseModel):
    nodes: list[ResolvedNode] = Field(default_factory=list)
    edges: list[ResolvedEdge] = Field(default_factory=list)

    source_run: str
    source_schema_version: str = SCHEMA_VERSION
    source_prompt_version: Optional[str] = None
    dedup_schema_version: str = DEDUP_SCHEMA_VERSION
    stage_version: str = "0.1.0"
    timestamp: datetime

    @model_validator(mode="after")
    def _referential_integrity(self) -> "ResolvedGraph":
        type_by_id = {n.id: n.type for n in self.nodes}
        node_ids = set(type_by_id)
        dangling = [
            f"{e.source_id}->{e.target_id}"
            for e in self.edges
            if e.source_id not in node_ids or e.target_id not in node_ids
        ]
        if dangling:
            raise ValueError(
                f"archi con estremi inesistenti fra i nodi: "
                f"{dangling[:10]}{'...' if len(dangling) > 10 else ''}"
            )

        type_mismatch: list[str] = []
        compatibility_violations: list[str] = []
        for e in self.edges:
            expected_src = type_by_id[e.source_id]
            expected_tgt = type_by_id[e.target_id]
            if e.source_type != expected_src:
                type_mismatch.append(
                    f"{e.source_id}: edge.source_type={e.source_type.value} "
                    f"node.type={expected_src.value}"
                )
            if e.target_type != expected_tgt:
                type_mismatch.append(
                    f"{e.target_id}: edge.target_type={e.target_type.value} "
                    f"node.type={expected_tgt.value}"
                )
            if not is_edge_valid(e.type, e.source_type, e.target_type):
                compatibility_violations.append(
                    f"{e.source_type.value} -[{e.type.value}]-> {e.target_type.value}"
                )
        if type_mismatch:
            raise ValueError(
                f"source_type/target_type non allineati ai nodi: "
                f"{type_mismatch[:10]}{'...' if len(type_mismatch) > 10 else ''}"
            )
        if compatibility_violations:
            raise ValueError(
                f"archi che violano EDGE_COMPATIBILITY: "
                f"{compatibility_violations[:10]}"
                f"{'...' if len(compatibility_violations) > 10 else ''}"
            )
        return self


def align_edge_endpoint_types(graph: ResolvedGraph) -> ResolvedGraph:
    """Imposta source_type e target_type dagli id dei nodi canonici (deterministico)."""
    type_by_id = {n.id: n.type for n in graph.nodes}
    aligned_edges = [
        e.model_copy(
            update={
                "source_type": type_by_id[e.source_id],
                "target_type": type_by_id[e.target_id],
            }
        )
        for e in graph.edges
    ]
    return graph.model_copy(update={"edges": aligned_edges})


# -----------------------------------------------------------------------------
# Stage6Proposal — vive nel log, NON nel grafo.
# -----------------------------------------------------------------------------

class Stage6Proposal(BaseModel):
    kind: str
    source_id: str
    target_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    note: Optional[str] = None


# -----------------------------------------------------------------------------
# Stage4Log — tracciamento completo di cosa è successo nello stadio 4.
# Documento separato dal grafo, è il "diario di bordo" del run.
# -----------------------------------------------------------------------------

class MergeApplied(BaseModel):
    canonical_id: str
    losers: list[str]
    method: str
    confidence: float


class SplitApplied(BaseModel):
    original_id: str
    new_ids_by_type: dict[str, str]


class Warning(BaseModel):
    kind: str            # es. "self_loop", "orphan_edge"
    detail: str


class Stats(BaseModel):
    input_chunks: int
    input_node_occurrences: int
    input_edge_occurrences: int
    output_nodes: int
    output_edges: int
    provenances_per_node_median: int
    provenances_per_node_p95: int
    provenances_per_node_max: int
    top_nodes: list[dict]   # [{"id", "type", "n_provenances"}]


class Stage4Log(BaseModel):
    source_run: str
    timestamp: datetime
    stage_version: str = "0.1.0"
    dedup_schema_version: str = DEDUP_SCHEMA_VERSION

    merges_applied: list[MergeApplied] = Field(default_factory=list)
    splits_applied: list[SplitApplied] = Field(default_factory=list)
    stage6_proposals: list[Stage6Proposal] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    stats: Stats