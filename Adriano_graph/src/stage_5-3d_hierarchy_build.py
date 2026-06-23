# src/stage_5-3d_hierarchy_build.py
"""
Stadio 5-3d — Costruzione della gerarchia tematica (ULTIMO pezzo del 5-3).

Prende il grafo consolidato del 5-2c e ci posa sopra la gerarchia tematica:
- archi SPECIALIZES (Theme -> Theme, specifico -> generale);
- nodi cappello con is_macro=True (Theme esistenti promossi o nuovi sintetizzati).

Tre fonti di archi SPECIALIZES, in ordine di autorità:
1. SEED DAG (5-3a): i 15 refinement già giudicati dal 5-2b, resi aciclici.
   Autoritativi: in caso di conflitto di direzione vincono.
2. JUDGMENTS (5-3b): per ogni cappello di cluster, membro -> cap_id.
3. ASSIGNMENTS (5-3c): per ogni orfano "specializes", orfano -> cap_id.

Pipeline interna:
- raccoglie gli archi dalle tre fonti, dedup per (source, target) tenendo la
  confidence migliore e marcando l'origine; il seed ha priorità sulla direzione;
- rompe i cicli del grafo COMBINATO (il seed è già aciclico, ma unendo le fonti
  possono nascere cicli) rimuovendo l'arco a confidence minima, come il 5-3a;
- i cappelli = tutti i nodi che sono TARGET di un SPECIALIZES. Quelli già nel
  grafo vengono promossi (is_macro=True, provenienze reali intatte); quelli
  inesistenti (nuovi, sintetizzati dal 5-3b) vengono creati come Theme con
  is_macro=True e provenienza sintetica ancorata a un chunk di un sotto-tema;
- costruisce gli archi come ResolvedEdge (Theme->Theme) con provenienza
  sintetica, e valida tutto con ResolvedGraph (referential integrity +
  EDGE_COMPATIBILITY: richiede SCHEMA_VERSION >= 0.3.0 con SPECIALIZES).

NON fa node-merge. Il conflitto anima/corpo (cluster #7: anima_e_corpo e
corpo_e_anima sono "same" ma il 5-2c li ha lasciati separati come conflict)
NON viene fuso qui: entrambi ricevono SPECIALIZES verso il loro cappello
("Il corpo") e restano due nodi. La loro fusione è un'operazione di 5-2c
(promozione del conflict_cluster), tracciata come punto aperto.

Input  (default)
- data/stage_5/2_themes/enriched_graph.json      (5-2c): grafo consolidato
- data/stage_5/3_hierarchy/hierarchy_candidates.json (5-3a): seed_edges
- data/stage_5/3_hierarchy/hierarchy_judgments.json  (5-3b): cappelli + membri
- data/stage_5/3_hierarchy/hierarchy_assignments.json(5-3c): orfani agganciati
Output (default: data/stage_5/3_hierarchy/)
- enriched_graph.json   (ResolvedGraph: grafo + SPECIALIZES + is_macro)
- hierarchy_map.json     (decisioni ispezionabili: archi per origine, cappelli
                          creati/promossi, cicli rotti, conteggi)

Idempotenza: funzione pura degli artefatti 5-2c/5-3a/b/c. Rilanciare riproduce
lo stesso grafo (nessun LLM qui).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from src.schema import EdgeType, NodeType, Provenance, SCHEMA_VERSION, is_edge_valid
from src.deduplication_schema import (
    DEDUP_SCHEMA_VERSION,
    ResolvedEdge,
    ResolvedGraph,
    ResolvedNode,
    align_edge_endpoint_types,
)

STAGE_VERSION = "0.1.0"
ENRICH_MODEL = "stage_5-3_hierarchy"

# Confidence di default quando la fonte non la porta esplicita.
_DEFAULT_SEED_CONF = 0.85
_DEFAULT_JUDGMENT_CONF = 0.80
_SYNTH_CAP_CONF = 0.70

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_HIER = _PROJECT_ROOT / "data" / "stage_5" / "3_hierarchy"
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "2_themes" / "enriched_graph.json"
DEFAULT_CANDIDATES = _HIER / "hierarchy_candidates.json"
DEFAULT_JUDGMENTS = _HIER / "hierarchy_judgments.json"
DEFAULT_ASSIGNMENTS = _HIER / "hierarchy_assignments.json"
DEFAULT_OUT_DIR = _HIER


# -----------------------------------------------------------------------------
# Modelli mappa
# -----------------------------------------------------------------------------

class SpecializesDecision(BaseModel):
    source_id: str
    target_id: str
    confidence: Optional[float] = None
    origins: list[str] = Field(default_factory=list)   # seed | judgment | assignment


class CapDecision(BaseModel):
    cap_id: str
    name: str
    action: str          # promoted | synthesized
    n_children: int


class HierarchyMap(BaseModel):
    source_graph: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    dedup_schema_version: str = DEDUP_SCHEMA_VERSION
    schema_version: str = SCHEMA_VERSION
    n_specializes: int
    n_caps_promoted: int
    n_caps_synthesized: int
    removed_cycle_edges: list[SpecializesDecision] = Field(default_factory=list)
    specializes: list[SpecializesDecision] = Field(default_factory=list)
    caps: list[CapDecision] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Raccolta archi dalle tre fonti
# -----------------------------------------------------------------------------

def collect_raw_edges(candidates: dict, judgments: dict,
                      assignments: dict) -> dict[tuple[str, str], dict]:
    """
    (source, target) -> {"confidence", "origins": set}.
    source = specifico, target = generale. Dedup tenendo la confidence migliore
    e l'unione delle origini. Direzione del seed prioritaria (vedi break_cycles).
    """
    raw: dict[tuple[str, str], dict] = {}

    def add(src: str, tgt: str, conf: Optional[float], origin: str):
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt)
        cur = raw.get(key)
        c = conf if conf is not None else None
        if cur is None:
            raw[key] = {"confidence": c, "origins": {origin}}
        else:
            cur["origins"].add(origin)
            if c is not None and (cur["confidence"] is None or c > cur["confidence"]):
                cur["confidence"] = c

    for e in candidates.get("seed_edges", []):
        add(e["specifico"], e["generale"],
            e.get("confidence") if e.get("confidence") is not None else _DEFAULT_SEED_CONF,
            "seed")
    for j in judgments.get("judgments", []):
        for cap in j.get("caps", []):
            cap_id = cap["cap_id"]
            for m in cap.get("members", []):
                add(m, cap_id, _DEFAULT_JUDGMENT_CONF, "judgment")
    for a in assignments.get("assignments", []):
        if a.get("decision") == "specializes" and a.get("cap_id"):
            add(a["orphan_id"], a["cap_id"], a.get("confidence"), "assignment")

    return raw


# -----------------------------------------------------------------------------
# Rottura cicli (Kahn + rimozione arco a confidence minima), come 5-3a
# -----------------------------------------------------------------------------

def break_cycles(edges: list[tuple[str, str, Optional[float]]]
                 ) -> tuple[list[tuple[str, str, Optional[float]]],
                            list[tuple[str, str, Optional[float]]]]:
    edges = list(edges)
    removed: list[tuple[str, str, Optional[float]]] = []
    while True:
        nodes: set[str] = set()
        outadj: dict[str, list[str]] = defaultdict(list)
        indeg: dict[str, int] = defaultdict(int)
        for s, d, _ in edges:
            nodes.add(s); nodes.add(d)
            outadj[s].append(d); indeg[d] += 1
        for n in nodes:
            indeg.setdefault(n, 0)
        indeg2 = dict(indeg)
        dq = deque([n for n in nodes if indeg2[n] == 0])
        seen = 0
        while dq:
            n = dq.popleft(); seen += 1
            for d in outadj[n]:
                indeg2[d] -= 1
                if indeg2[d] == 0:
                    dq.append(d)
        if seen == len(nodes):
            return edges, removed
        cyc = {n for n in nodes if indeg2[n] > 0}
        cand = [(i, e) for i, e in enumerate(edges) if e[0] in cyc and e[1] in cyc]
        i_rem, e_rem = min(cand, key=lambda x: (x[1][2] if x[1][2] is not None else 0.0))
        removed.append(e_rem)
        edges.pop(i_rem)


# -----------------------------------------------------------------------------
# Helper grafo
# -----------------------------------------------------------------------------

def _theme_nodes(graph: ResolvedGraph) -> dict[str, ResolvedNode]:
    return {n.id: n for n in graph.nodes if n.type == NodeType.THEME}


def _first_chunk(node: ResolvedNode) -> Optional[str]:
    chunks = sorted({p.chunk_id for p in node.provenances})
    return chunks[0] if chunks else None


def _cap_label(cap_id: str, judgments: dict, theme_nodes: dict[str, ResolvedNode]) -> str:
    if cap_id in theme_nodes:
        return theme_nodes[cap_id].name
    for j in judgments.get("judgments", []):
        for cap in j.get("caps", []):
            if cap["cap_id"] == cap_id and cap.get("label"):
                return cap["label"]
    return cap_id


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(graph_path: Path, candidates_path: Path, judgments_path: Path,
        assignments_path: Path, out_dir: Path, dry_run: bool = False) -> HierarchyMap:
    with graph_path.open("r", encoding="utf-8") as f:
        graph = ResolvedGraph(**json.load(f))
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    judgments = json.loads(judgments_path.read_text(encoding="utf-8"))
    assignments = (json.loads(assignments_path.read_text(encoding="utf-8"))
                   if assignments_path.exists() else {"assignments": []})

    theme_nodes = _theme_nodes(graph)
    now = datetime.now(timezone.utc)

    # 1) raccolta + dedup
    raw = collect_raw_edges(candidates, judgments, assignments)

    # 2) rottura cicli sul grafo combinato
    edge_list = [(s, t, raw[(s, t)]["confidence"]) for (s, t) in raw]
    acyclic, removed = break_cycles(edge_list)
    acyclic_keys = {(s, t) for s, t, _ in acyclic}

    # 3) cappelli = tutti i target degli archi rimasti
    cap_ids = sorted({t for _, t, _ in acyclic})
    children_count: dict[str, int] = defaultdict(int)
    for _, t, _ in acyclic:
        children_count[t] += 1

    # 3a) sorgenti per ancorare la provenienza dei cappelli nuovi
    incoming_src: dict[str, list[str]] = defaultdict(list)
    for s, t, _ in acyclic:
        incoming_src[t].append(s)

    # 4) crea/promuovi i nodi cappello
    promoted, synthesized = [], []
    new_nodes_by_id: dict[str, ResolvedNode] = {}
    for cap_id in cap_ids:
        if cap_id in theme_nodes:
            promoted.append(cap_id)
        else:
            # sintetizza: provenienza ancorata a un chunk di un sotto-tema
            ground = None
            for src in incoming_src.get(cap_id, []):
                if src in theme_nodes:
                    ground = _first_chunk(theme_nodes[src])
                    if ground:
                        break
            label = _cap_label(cap_id, judgments, theme_nodes)
            prov = Provenance(
                chunk_id=ground or "UNKNOWN",
                model=ENRICH_MODEL,
                timestamp=now,
                schema_version=SCHEMA_VERSION,
                confidence=_SYNTH_CAP_CONF,
                evidence_span=None,
                human_validated=False,
            )
            node = ResolvedNode(
                id=cap_id,
                type=NodeType.THEME,
                name=label,
                description=f"Tema-cappello sintetizzato nello stadio 5-3 che "
                            f"raccoglie i sotto-temi affini ({children_count[cap_id]} figli).",
                aliases=[],
                provenances=[prov],
                merged_from=[cap_id],
                merge_method="none",
                merge_confidence=_SYNTH_CAP_CONF,
                is_macro=True,
                review_needed=True,
                review_reason="cappello tematico sintetizzato in 5-3d (nodo nuovo, non estratto)",
            )
            new_nodes_by_id[cap_id] = node
            synthesized.append(cap_id)

    # 5) lista nodi finale: promuovi gli esistenti (is_macro=True), aggiungi i nuovi
    cap_set = set(cap_ids)
    final_nodes: list[ResolvedNode] = []
    for n in graph.nodes:
        if n.type == NodeType.THEME and n.id in cap_set and not n.is_macro:
            final_nodes.append(n.model_copy(update={"is_macro": True}))
        else:
            final_nodes.append(n)
    final_nodes.extend(new_nodes_by_id.values())

    node_by_id = {n.id: n for n in final_nodes}

    # 6) archi SPECIALIZES come ResolvedEdge
    spec_edges: list[ResolvedEdge] = []
    spec_decisions: list[SpecializesDecision] = []
    for s, t, conf in acyclic:
        src_node = node_by_id.get(s)
        ground = _first_chunk(src_node) if src_node else None
        origins = sorted(raw[(s, t)]["origins"])
        prov = Provenance(
            chunk_id=ground or "UNKNOWN",
            model=ENRICH_MODEL,
            timestamp=now,
            schema_version=SCHEMA_VERSION,
            confidence=conf,
            evidence_span=None,
            human_validated=False,
        )
        spec_edges.append(ResolvedEdge(
            source_id=s, target_id=t, type=EdgeType.SPECIALIZES,
            source_type=NodeType.THEME, target_type=NodeType.THEME,
            description=f"Gerarchia tematica (origine: {', '.join(origins)}).",
            role=None, provenances=[prov],
            merged_from=[f"stage5_specializes:{s}|SPECIALIZES|{t}"],
            merge_confidence=conf if conf is not None else _DEFAULT_JUDGMENT_CONF,
            review_needed=False, review_reason=None,
        ))
        spec_decisions.append(SpecializesDecision(
            source_id=s, target_id=t, confidence=conf, origins=origins))

    # 7) assembla e valida (referential integrity + EDGE_COMPATIBILITY)
    enriched = ResolvedGraph(
        nodes=final_nodes,
        edges=graph.edges + spec_edges,
        source_run=graph.source_run,
        source_schema_version=graph.source_schema_version,
        source_prompt_version=graph.source_prompt_version,
        dedup_schema_version=DEDUP_SCHEMA_VERSION,
        stage_version=graph.stage_version,
        timestamp=now,
    )
    enriched = align_edge_endpoint_types(enriched)

    hmap = HierarchyMap(
        source_graph=str(graph_path), timestamp=now,
        n_specializes=len(spec_edges),
        n_caps_promoted=len(promoted), n_caps_synthesized=len(synthesized),
        removed_cycle_edges=[SpecializesDecision(source_id=s, target_id=t, confidence=c,
                                                 origins=sorted(raw.get((s, t), {}).get("origins", [])))
                             for s, t, c in removed],
        specializes=spec_decisions,
        caps=[CapDecision(cap_id=c, name=_cap_label(c, judgments, theme_nodes),
                          action="promoted" if c in set(promoted) else "synthesized",
                          n_children=children_count[c])
              for c in cap_ids],
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "enriched_graph.json").write_text(
            enriched.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / "hierarchy_map.json").write_text(
            hmap.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(hmap, removed)
    return hmap


def _print_summary(h: HierarchyMap, removed) -> None:
    print("stage 5-3d — costruzione gerarchia tematica")
    print(f"  archi SPECIALIZES posati : {h.n_specializes}")
    print(f"  cicli rotti              : {len(removed)}")
    for e in h.removed_cycle_edges:
        print(f"    [rotto] {e.source_id} -> {e.target_id} (conf {e.confidence})")
    print(f"  cappelli promossi (esistenti) : {h.n_caps_promoted}")
    print(f"  cappelli sintetizzati (nuovi) : {h.n_caps_synthesized}")
    top = sorted(h.caps, key=lambda c: -c.n_children)[:12]
    print("  cappelli con più figli:")
    for c in top:
        print(f"    {c.n_children:>2} <- {c.cap_id}  ({c.name})  [{c.action}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-3d: costruisce la gerarchia tematica (SPECIALIZES + is_macro).")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="enriched_graph.json del 5-2c")
    ap.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES, help="hierarchy_candidates.json (5-3a)")
    ap.add_argument("--judgments", type=Path, default=DEFAULT_JUDGMENTS, help="hierarchy_judgments.json (5-3b)")
    ap.add_argument("--assignments", type=Path, default=DEFAULT_ASSIGNMENTS, help="hierarchy_assignments.json (5-3c)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="cartella di output")
    ap.add_argument("--dry-run", action="store_true", help="non scrive file, stampa solo il riepilogo")
    args = ap.parse_args()
    run(args.graph, args.candidates, args.judgments, args.assignments, args.out, dry_run=args.dry_run)


if __name__ == "__main__":
    main()