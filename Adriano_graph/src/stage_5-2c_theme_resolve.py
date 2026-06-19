# src/stage_5-2c_theme_resolve.py
"""
Stadio 5-2c — Resolver del consolidamento Theme.

Terzo pezzo del 5-2. Prende i giudizi del 5-2b e applica i merge dei `same` al
grafo, con la policy dell'ADR-023. NON crea archi SPECIALIZES né cappelli
(è il 5-3): qui si fondono solo i temi `same` e si preparano i `refinement`
rimappati per il 5-3.

Policy (ADR-023 punto 6)
------------------------
- Cluster = componenti connesse sui `same` (non per coppia).
- Conflitto same×refinement INTERNO: se un `refinement` ha ENTRAMBI gli estremi
  nello stesso cluster, il cluster NON si fonde -> conflict_clusters.
  (Un refinement con un solo estremo nel cluster è legittimo: si rimappa.)
- cluster_confidence = min delle confidence dei `same` interni.
  >= --min-confidence (default 0.97) e senza conflitto -> merge SILENZIOSO.
  < soglia e senza conflitto -> review_clusters (NON applicato).
- Merge: id/name/description canonici per frequenza (n. provenienze, tie-break
  alfabetico), mai riscritti; provenienze tutte accorpate; nomi alternativi in
  aliases; merge_method="synonym_llm"; archi rimappati e dedup, self-loop tolti.
- Refinement rimappati sugli id canonici (estremi in cluster applicati) e
  passati al 5-3.

Input
-----
- enriched_graph.json   (5-1, ResolvedGraph): grafo corrente.
- theme_judgments.json  (5-2b): verdetti same/refinement/distinct.

Output (default: data/stage_5/2_themes/)
- enriched_graph.json   (grafo con i Theme `same` fusi) -> input del 5-3
- theme_merge_map.json   (decisioni ispezionabili)

PREREQUISITO: applicare i bump di schema dell'ADR-023 (is_macro su ResolvedNode,
SPECIALIZES in EdgeType) PRIMA di eseguire. Il 5-2c non crea SPECIALIZES ma il
grafo a valle li userà; `is_macro` deve esistere sul modello.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from src.schema import EdgeType, Provenance
from src.deduplication_schema import (
    DEDUP_SCHEMA_VERSION,
    ResolvedEdge,
    ResolvedGraph,
    ResolvedNode,
)

STAGE_VERSION = "0.1.0"
DEFAULT_MIN_CONFIDENCE = 0.97

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_THEMES_DIR = _PROJECT_ROOT / "data" / "stage_5" / "2_themes"
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "1_embodies" / "enriched_graph.json"
DEFAULT_JUDGMENTS = _THEMES_DIR / "theme_judgments.json"
DEFAULT_OUT_GRAPH = _THEMES_DIR / "enriched_graph.json"
DEFAULT_OUT_MAP = _THEMES_DIR / "theme_merge_map.json"


# -----------------------------------------------------------------------------
# Modelli mappa decisioni
# -----------------------------------------------------------------------------

class MergeCluster(BaseModel):
    canonical_id: str
    members: list[str]
    member_freqs: dict[str, int]
    cluster_confidence: float
    applied: bool
    review_needed: bool
    reason: Optional[str] = None


class ConflictCluster(BaseModel):
    members: list[str]
    internal_refinements: list[dict]  # [{specifico, generale}]
    reason: str


class RemappedRefinement(BaseModel):
    specifico: str
    generale: str
    confidence: Optional[float] = None
    remapped: bool = False


class ThemeMergeMap(BaseModel):
    source_graph: str
    source_judgments: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    dedup_schema_version: str = DEDUP_SCHEMA_VERSION
    min_confidence: float
    n_same: int
    n_refinement: int
    n_error: int
    applied_clusters: list[MergeCluster] = Field(default_factory=list)
    review_clusters: list[MergeCluster] = Field(default_factory=list)
    conflict_clusters: list[ConflictCluster] = Field(default_factory=list)
    refinements_for_5_3: list[RemappedRefinement] = Field(default_factory=list)
    nodes_before: int
    nodes_after: int
    edges_before: int
    edges_after: int
    edges_dropped_selfloop: int


# -----------------------------------------------------------------------------
# Union-Find
# -----------------------------------------------------------------------------

class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# -----------------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------------

def _prov_key(p: Provenance) -> tuple:
    return (p.chunk_id, p.model, p.timestamp.isoformat(), p.evidence_span)


def _dedup_provs(provs: list[Provenance]) -> list[Provenance]:
    seen: set[tuple] = set()
    out: list[Provenance] = []
    for p in provs:
        k = _prov_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def load_graph(path: Path) -> ResolvedGraph:
    with path.open("r", encoding="utf-8") as f:
        return ResolvedGraph(**json.load(f))


def load_judgments(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def _promote_manual(
    out_map: Path,
    review_clusters: list[MergeCluster],
    applied_clusters: list[MergeCluster],
    canonical_map: dict[str, str],
) -> int:
    """
    Legge le promozioni manuali dal theme_merge_map.json esistente: i
    review_clusters con `applied: true` vengono spostati ad applied_clusters
    (reason "manually approved"), rispettando il canonical_id scelto a mano
    (deve essere uno dei membri). Aggiorna canonical_map. Ritorna il numero di
    cluster promossi. Il match coi review correnti è per insieme di membri,
    così resta robusto anche se il canonical_id è stato cambiato.
    """
    if not out_map.exists():
        raise SystemExit(
            f"--update richiede una mappa esistente in {out_map}. "
            "Esegui prima il 5-2c senza flag, poi edita la mappa."
        )
    saved = json.loads(out_map.read_text(encoding="utf-8"))
    by_members = {frozenset(c.members): c for c in review_clusters}
    promoted = 0
    for rc in saved.get("review_clusters", []):
        if not rc.get("applied"):
            continue
        members = rc.get("members", [])
        canonical = rc.get("canonical_id")
        fresh = by_members.get(frozenset(members))
        if fresh is None:
            print(f"  [update] cluster {members} non è fra i review correnti, ignorato")
            continue
        if canonical not in members:
            print(f"  [update] canonical_id {canonical!r} non è fra i membri {members}; "
                  "cluster ignorato (correggi e rilancia)")
            continue
        applied_clusters.append(MergeCluster(
            canonical_id=canonical, members=fresh.members, member_freqs=fresh.member_freqs,
            cluster_confidence=fresh.cluster_confidence, applied=True,
            review_needed=False, reason="manually approved",
        ))
        for m in members:
            if m != canonical:
                canonical_map[m] = canonical
        review_clusters.remove(fresh)
        promoted += 1
    return promoted


def run(
    graph_path: Path,
    judgments_path: Path,
    out_graph: Path,
    out_map: Path,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    update: bool = False,
    dry_run: bool = False,
) -> ThemeMergeMap:
    graph = load_graph(graph_path)
    jdata = load_judgments(judgments_path)
    judgments = jdata.get("judgments", [])

    node_index: dict[str, ResolvedNode] = {n.id: n for n in graph.nodes}

    same = [(j["theme_a"], j["theme_b"], j.get("confidence"))
            for j in judgments if j.get("verdict") == "same"]
    refinements = [(j["theme_a"], j["theme_b"], j.get("general_id"), j.get("confidence"))
                   for j in judgments if j.get("verdict") == "refinement" and j.get("general_id")]
    n_error = sum(1 for j in judgments if j.get("verdict") == "error")

    # 1) componenti connesse sui `same`
    uf = _UF()
    for a, b, conf in same:
        uf.union(a, b)
    comps: dict[str, list[str]] = {}
    for a, b, _ in same:
        for x in (a, b):
            root = uf.find(x)
            if x not in comps.get(root, []):
                comps.setdefault(root, [])
                if x not in comps[root]:
                    comps[root].append(x)
    # confidence per componente = min delle same interne
    comp_conf: dict[str, float] = {}
    for a, b, conf in same:
        root = uf.find(a)
        c = conf if conf is not None else 0.0
        comp_conf[root] = min(comp_conf.get(root, 1.0), c)

    # 2) refinement interni a una componente = conflitto
    conflict_roots: dict[str, list[dict]] = {}
    for a, b, gid, _ in refinements:
        if a in uf.p and b in uf.p and uf.find(a) == uf.find(b):
            root = uf.find(a)
            specifico = a if gid == b else b
            conflict_roots.setdefault(root, []).append(
                {"specifico": specifico, "generale": gid})

    # 3) classificazione cluster + mappa canonica (solo cluster applicati)
    applied_clusters: list[MergeCluster] = []
    review_clusters: list[MergeCluster] = []
    conflict_clusters: list[ConflictCluster] = []
    canonical_map: dict[str, str] = {}  # loser_id -> canonical_id (solo applicati)

    def _freq(tid: str) -> int:
        n = node_index.get(tid)
        return len(n.provenances) if n else 0

    for root in sorted(comps):
        members = sorted(comps[root])
        # ignora membri non presenti come nodi (dovrebbe non capitare)
        members = [m for m in members if m in node_index]
        if len(members) < 2:
            continue
        freqs = {m: _freq(m) for m in members}
        conf = round(comp_conf.get(root, 0.0), 4)
        # canonical deterministico: max frequenza, tie-break alfabetico
        canonical = sorted(members, key=lambda m: (-freqs[m], m))[0]

        if root in conflict_roots:
            conflict_clusters.append(ConflictCluster(
                members=members,
                internal_refinements=conflict_roots[root],
                reason="refinement interno al cluster: stessa relazione giudicata sia same sia refinement",
            ))
            continue

        if conf >= min_confidence:
            applied_clusters.append(MergeCluster(
                canonical_id=canonical, members=members, member_freqs=freqs,
                cluster_confidence=conf, applied=True, review_needed=False,
            ))
            for m in members:
                if m != canonical:
                    canonical_map[m] = canonical
        else:
            review_clusters.append(MergeCluster(
                canonical_id=canonical, members=members, member_freqs=freqs,
                cluster_confidence=conf, applied=False, review_needed=True,
                reason=f"cluster_confidence {conf} < soglia {min_confidence}",
            ))

    # 3-bis) --update: applica le promozioni manuali fatte a mano nella mappa.
    # I review_clusters marcati `applied: true` nel theme_merge_map.json vengono
    # promossi ad applied; il canonical_id scelto a mano è rispettato (purché sia
    # un membro del cluster). Si riparte SEMPRE dal grafo sorgente (5-1), mai dal
    # grafo già fuso, quindi --update è idempotente.
    promoted = 0
    if update:
        promoted = _promote_manual(out_map, review_clusters, applied_clusters, canonical_map)

    # 4) applica i merge ai nodi
    new_nodes: list[ResolvedNode] = []
    for n in graph.nodes:
        if n.id in canonical_map:
            continue  # loser: assorbito nel canonico
        new_nodes.append(n)
    # sostituisci i nodi canonici con la versione fusa
    canon_to_members: dict[str, list[str]] = {}
    for loser, canon in canonical_map.items():
        canon_to_members.setdefault(canon, []).append(loser)

    for i, n in enumerate(new_nodes):
        if n.id in canon_to_members:
            members = [n.id] + canon_to_members[n.id]
            member_nodes = [node_index[m] for m in members]
            all_provs = _dedup_provs([p for mn in member_nodes for p in mn.provenances])
            alias_set = set(n.aliases)
            for mn in member_nodes:
                alias_set.update(mn.aliases)
                if mn.name != n.name:
                    alias_set.add(mn.name)
            cluster = next(c for c in applied_clusters if c.canonical_id == n.id)
            new_nodes[i] = ResolvedNode(
                id=n.id, type=n.type, name=n.name, description=n.description,
                aliases=sorted(alias_set),
                provenances=all_provs,
                merged_from=sorted(set(n.merged_from) | set(members)),
                merge_method="synonym_llm",
                merge_confidence=cluster.cluster_confidence,
                review_needed=False,
            )

    new_index = {n.id: n for n in new_nodes}

    # 5) rimappa archi, dedup, togli self-loop
    def _remap(tid: str) -> str:
        return canonical_map.get(tid, tid)

    acc: dict[tuple, ResolvedEdge] = {}
    dropped_selfloop = 0
    for e in graph.edges:
        s, t = _remap(e.source_id), _remap(e.target_id)
        if s == t:
            dropped_selfloop += 1
            continue
        key = (s, e.type, t, e.role)
        if key in acc:
            prev = acc[key]
            acc[key] = prev.model_copy(update={
                "provenances": _dedup_provs(prev.provenances + e.provenances),
                "merged_from": sorted(set(prev.merged_from) | set(e.merged_from)),
                "review_needed": prev.review_needed or e.review_needed,
            })
        else:
            acc[key] = e.model_copy(update={
                "source_id": s, "target_id": t,
                "source_type": new_index[s].type, "target_type": new_index[t].type,
            })
    new_edges = list(acc.values())

    # 6) refinement rimappati per il 5-3 (esclusi quelli interni ai conflitti)
    conflict_pairs = {(r["specifico"], r["generale"]) for c in conflict_clusters
                      for r in c.internal_refinements}
    refs_out: list[RemappedRefinement] = []
    seen_ref: set[tuple] = set()
    for a, b, gid, conf in refinements:
        specifico = a if gid == b else b
        generale = gid
        if (specifico, generale) in conflict_pairs:
            continue
        rs, rg = _remap(specifico), _remap(generale)
        if rs == rg:
            continue
        if (rs, rg) in seen_ref:
            continue
        seen_ref.add((rs, rg))
        refs_out.append(RemappedRefinement(
            specifico=rs, generale=rg, confidence=conf,
            remapped=(rs != specifico or rg != generale),
        ))

    now = datetime.now(timezone.utc)
    mmap = ThemeMergeMap(
        source_graph=str(graph_path), source_judgments=str(judgments_path),
        timestamp=now, min_confidence=min_confidence,
        n_same=len(same), n_refinement=len(refinements), n_error=n_error,
        applied_clusters=applied_clusters, review_clusters=review_clusters,
        conflict_clusters=conflict_clusters, refinements_for_5_3=refs_out,
        nodes_before=len(graph.nodes), nodes_after=len(new_nodes),
        edges_before=len(graph.edges), edges_after=len(new_edges),
        edges_dropped_selfloop=dropped_selfloop,
    )

    if not dry_run:
        # validazione: ricostruzione ResolvedGraph (referential integrity +
        # EDGE_COMPATIBILITY + allineamento tipi) gira in __init__.
        enriched = ResolvedGraph(
            nodes=new_nodes, edges=new_edges,
            source_run=graph.source_run,
            source_schema_version=graph.source_schema_version,
            source_prompt_version=graph.source_prompt_version,
            dedup_schema_version=DEDUP_SCHEMA_VERSION,
            stage_version=graph.stage_version,
            timestamp=now,
        )
        out_graph.parent.mkdir(parents=True, exist_ok=True)
        out_graph.write_text(enriched.model_dump_json(indent=2), encoding="utf-8")
        out_map.write_text(mmap.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(mmap, dry_run, update, promoted)
    return mmap


def _print_summary(m: ThemeMergeMap, dry_run: bool, update: bool, promoted: int) -> None:
    tag = "[DRY-RUN] " if dry_run else ""
    mode = "  [--update]" if update else ""
    print(f"{tag}stage 5-2c — consolidamento Theme  (soglia merge {m.min_confidence}){mode}")
    print(f"  giudizi: same={m.n_same}  refinement={m.n_refinement}  error={m.n_error}")
    print(f"  nodi: {m.nodes_before} -> {m.nodes_after}   archi: {m.edges_before} -> {m.edges_after}"
          f"  (self-loop tolti: {m.edges_dropped_selfloop})")
    silent = [c for c in m.applied_clusters if c.reason != "manually approved"]
    manual = [c for c in m.applied_clusters if c.reason == "manually approved"]
    print(f"  cluster applicati (merge silenzioso): {len(silent)}")
    for c in silent:
        print(f"    [{c.cluster_confidence:.2f}] {c.canonical_id}  <=  "
              f"{[x for x in c.members if x != c.canonical_id]}")
    if update or manual:
        print(f"  cluster applicati a mano (--update): {len(manual)}")
        for c in manual:
            print(f"    [{c.cluster_confidence:.2f}] {c.canonical_id}  <=  "
                  f"{[x for x in c.members if x != c.canonical_id]}  (manually approved)")
    print(f"  cluster in review (NON applicati): {len(m.review_clusters)}")
    for c in m.review_clusters:
        print(f"    [{c.cluster_confidence:.2f}] proposto canonical={c.canonical_id}  ?  {c.members}")
    print(f"  cluster in conflitto (same x refinement): {len(m.conflict_clusters)}")
    for c in m.conflict_clusters:
        print(f"    {c.members}  | refinement interni: {c.internal_refinements}")
    print(f"  refinement passati al 5-3: {len(m.refinements_for_5_3)}")

    if update:
        print(f"\n  >> promossi a mano: {promoted}   rimasti in review: {len(m.review_clusters)}")
    elif not dry_run and m.review_clusters:
        print("\n" + "=" * 70)
        print("  AZIONE RICHIESTA — ci sono cluster in review non applicati.")
        print(f"  1. Apri:  {DEFAULT_OUT_MAP}")
        print('  2. Nei review_clusters che vuoi fondere, metti  "applied": true')
        print('     (e correggi "canonical_id" se preferisci un id diverso, purché sia un membro).')
        print("  3. Rilancia con il flag --update per applicare le tue scelte.")
        print("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-2c: resolver dei merge Theme.")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="enriched_graph.json (5-1)")
    ap.add_argument("--judgments", type=Path, default=DEFAULT_JUDGMENTS, help="theme_judgments.json (5-2b)")
    ap.add_argument("--out-graph", type=Path, default=DEFAULT_OUT_GRAPH)
    ap.add_argument("--out-map", type=Path, default=DEFAULT_OUT_MAP)
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                    help="soglia di merge silenzioso (default 0.97); sotto va in review")
    ap.add_argument("--update", action="store_true",
                    help="rilegge la mappa e applica i review_clusters marcati 'applied: true' a mano")
    ap.add_argument("--dry-run", action="store_true", help="non scrive file, stampa solo le decisioni")
    args = ap.parse_args()

    run(args.graph, args.judgments, args.out_graph, args.out_map,
        min_confidence=args.min_confidence, update=args.update, dry_run=args.dry_run)


if __name__ == "__main__":
    main()