# src/stage_5-3a_hierarchy_candidates.py
"""
Stadio 5-3a — Candidati per la gerarchia tematica.

Primo pezzo del 5-3. DETERMINISTICO, NIENTE LLM, NON modifica il grafo:
prepara il terreno per la gerarchia (archi SPECIALIZES + cappelli is_macro) che
verrà costruita nel 5-3b (naming/assegnazione via Qwen) e 5-3c (grafo).

Fa due cose:

1) SEED DAG dai refinement già giudicati (5-2b/5-2c). I `refinements_for_5_3`
   del theme_merge_map.json sono archi orientati specifico -> generale. Vengono
   assemblati in un grafo diretto; gli eventuali CICLI (il giudice ha valutato
   le coppie in isolamento, può aver prodotto A>B>C>A) vengono spezzati in modo
   deterministico: si rimuove l'arco a confidence più bassa del ciclo. Dal DAG
   risultante si individuano:
   - cappelli esistenti (cap): nodi che sono `generale` di qualcosa ma non
     `specifico` di nient'altro (in cima a una catena);
   - intermedi: nodi con archi sia entranti sia uscenti (es. il cluster
     identità).

2) CLUSTERING di TUTTI i temi (inclusi i ~290 orfani non toccati dai
   refinement) per similarità BGE-M3 sui `name`. Produce gruppi tematici
   candidati: dentro ciascun gruppo, nel 5-3b, Qwen proporrà/confermerà un
   cappello e l'assegnazione. Le soglie sono volutamente da tarare a vista
   sulla distribuzione delle dimensioni dei cluster stampata a fine run.

Input  (default: data/stage_5/2_themes/)
- enriched_graph.json   (5-2c): da cui i nodi Theme (id, name, freq).
- theme_merge_map.json  (5-2c): da cui `refinements_for_5_3`.
Output (default: data/stage_5/3_hierarchy/)
- hierarchy_candidates.json  (seed DAG + cluster, autoritativo)
- theme_clusters.tsv          (un tema per riga, per ispezione)

Embedding del `name` (come 5-2a): la `description` è specifica del chunk; per il
raggruppamento del tema generale conta il nome. Flag `--embed name_desc` per
l'alternativa.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from pydantic import BaseModel, Field

STAGE_VERSION = "0.1.0"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_DISTANCE = 0.45   # 1 - cosine; soglia di taglio del clustering agglomerativo

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_THEMES_DIR = _PROJECT_ROOT / "data" / "stage_5" / "2_themes"
DEFAULT_GRAPH = _THEMES_DIR / "enriched_graph.json"
DEFAULT_MERGE_MAP = _THEMES_DIR / "theme_merge_map.json"
DEFAULT_OUT_DIR = _PROJECT_ROOT / "data" / "stage_5" / "3_hierarchy"


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class SeedEdge(BaseModel):
    specifico: str
    generale: str
    confidence: Optional[float] = None


class ClusterMember(BaseModel):
    id: str
    name: str
    freq: int


class ThemeCluster(BaseModel):
    cluster_id: int
    size: int
    cohesion: float                      # coseno medio intra-cluster (1.0 se singoletto)
    members: list[ClusterMember]
    existing_caps: list[str] = Field(default_factory=list)      # cap del seed DAG presenti
    refinement_nodes: list[str] = Field(default_factory=list)   # nodi toccati dai refinement


class HierarchyCandidates(BaseModel):
    source_graph: str
    source_merge_map: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    model: str
    embed_text: str
    distance_threshold: float
    n_themes: int
    n_clusters: int
    n_singletons: int
    # seed DAG
    seed_edges: list[SeedEdge] = Field(default_factory=list)
    removed_cycle_edges: list[SeedEdge] = Field(default_factory=list)
    existing_caps: list[str] = Field(default_factory=list)
    intermediate_nodes: list[str] = Field(default_factory=list)
    # clustering
    clusters: list[ThemeCluster] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Lettura input
# -----------------------------------------------------------------------------

class _Theme(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    freq: int


def load_themes(graph_path: Path) -> list[_Theme]:
    with graph_path.open("r", encoding="utf-8") as f:
        graph = json.load(f)
    out: list[_Theme] = []
    for n in graph.get("nodes", []):
        if n.get("type") == "Theme":
            out.append(_Theme(id=n["id"], name=n["name"],
                              description=n.get("description"),
                              freq=len(n.get("provenances", []))))
    return out


def load_refinements(merge_map_path: Path) -> list[tuple[str, str, Optional[float]]]:
    """Ritorna (specifico, generale, confidence) dai refinements_for_5_3."""
    with merge_map_path.open("r", encoding="utf-8") as f:
        m = json.load(f)
    out = []
    for r in m.get("refinements_for_5_3", []):
        out.append((r["specifico"], r["generale"], r.get("confidence")))
    return out


# -----------------------------------------------------------------------------
# Seed DAG: rottura cicli deterministica (Kahn + rimozione arco a conf minima)
# -----------------------------------------------------------------------------

def break_cycles(
    edges: list[tuple[str, str, Optional[float]]]
) -> tuple[list[tuple[str, str, Optional[float]]], list[tuple[str, str, Optional[float]]]]:
    """
    Rende aciclico il grafo diretto specifico->generale. Finché esiste un ciclo
    (Kahn non processa tutti i nodi), rimuove l'arco a confidence più bassa tra
    quelli interamente dentro la parte ciclica. Input piccolo (~decine di archi).
    Ritorna (edges_acicliche, edges_rimosse).
    """
    edges = list(edges)
    removed: list[tuple[str, str, Optional[float]]] = []
    while True:
        nodes: set[str] = set()
        outadj: dict[str, list[str]] = defaultdict(list)
        indeg: dict[str, int] = defaultdict(int)
        for s, d, _ in edges:
            nodes.add(s); nodes.add(d)
            outadj[s].append(d)
            indeg[d] += 1
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


def caps_and_intermediates(
    edges: list[tuple[str, str, Optional[float]]]
) -> tuple[list[str], list[str]]:
    """
    Dal DAG specifico->generale:
    - cap esistente = nodo con archi ENTRANTI (è `generale` di qualcuno) e ZERO
      uscenti (non è `specifico` di nessuno): in cima a una catena.
    - intermedio = nodo con archi sia entranti sia uscenti.
    """
    out_src: set[str] = set()   # nodi che sono `specifico` (hanno arco uscente)
    in_tgt: set[str] = set()    # nodi che sono `generale` (hanno arco entrante)
    for s, d, _ in edges:
        out_src.add(s)
        in_tgt.add(d)
    caps = sorted(in_tgt - out_src)
    intermediates = sorted(in_tgt & out_src)
    return caps, intermediates


# -----------------------------------------------------------------------------
# Embedding + clustering
# -----------------------------------------------------------------------------

def embed_names(texts: list[str], model_name: str, device: Optional[str]) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("sentence-transformers non installato. "
                         "pip install sentence-transformers") from exc
    model = SentenceTransformer(model_name, device=device)
    emb = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                       show_progress_bar=True, batch_size=32)
    return emb.astype(np.float32)


def cluster_embeddings(emb: np.ndarray, distance_threshold: float) -> np.ndarray:
    """Clustering agglomerativo (average linkage, metrica coseno) senza K fisso."""
    try:
        from sklearn.cluster import AgglomerativeClustering
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("scikit-learn non installato (di norma arriva con "
                         "sentence-transformers). pip install scikit-learn") from exc
    if emb.shape[0] == 1:
        return np.array([0])
    try:
        model = AgglomerativeClustering(
            n_clusters=None, distance_threshold=distance_threshold,
            metric="cosine", linkage="average")
        return model.fit_predict(emb)
    except TypeError:
        # sklearn < 1.2 usa 'affinity' invece di 'metric'
        model = AgglomerativeClustering(
            n_clusters=None, distance_threshold=distance_threshold,
            affinity="cosine", linkage="average")
        return model.fit_predict(emb)


def _cohesion(emb: np.ndarray, idxs: list[int]) -> float:
    if len(idxs) < 2:
        return 1.0
    sub = emb[idxs]
    sims = sub @ sub.T
    n = len(idxs)
    # media dei coseni off-diagonale
    return float((sims.sum() - n) / (n * (n - 1)))


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(
    graph_path: Path,
    merge_map_path: Path,
    out_dir: Path,
    embed_text: str = "name",
    model_name: str = DEFAULT_MODEL,
    distance_threshold: float = DEFAULT_DISTANCE,
    device: Optional[str] = None,
    dry_run: bool = False,
) -> HierarchyCandidates:
    themes = load_themes(graph_path)
    if not themes:
        raise SystemExit(f"nessun nodo Theme in {graph_path}")
    refinements = load_refinements(merge_map_path)

    # 1) seed DAG
    acyclic, removed = break_cycles(refinements)
    caps, intermediates = caps_and_intermediates(acyclic)
    cap_set, refine_nodes = set(caps), {x for e in acyclic for x in (e[0], e[1])}

    # 2) clustering
    if embed_text == "name_desc":
        texts = [f"{t.name}. {t.description or ''}".strip() for t in themes]
    else:
        texts = [t.name for t in themes]
    emb = embed_names(texts, model_name, device)
    labels = cluster_embeddings(emb, distance_threshold)

    by_label: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        by_label[int(lab)].append(i)

    clusters: list[ThemeCluster] = []
    for lab, idxs in sorted(by_label.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        members = [themes[i] for i in idxs]
        ids = [m.id for m in members]
        clusters.append(ThemeCluster(
            cluster_id=lab,
            size=len(members),
            cohesion=round(_cohesion(emb, idxs), 4),
            members=[ClusterMember(id=m.id, name=m.name, freq=m.freq) for m in
                     sorted(members, key=lambda m: (-m.freq, m.id))],
            existing_caps=sorted(set(ids) & cap_set),
            refinement_nodes=sorted(set(ids) & refine_nodes),
        ))
    # rinumera i cluster_id in ordine di dimensione (stabilità ispezione)
    for new_id, c in enumerate(clusters):
        c.cluster_id = new_id

    report = HierarchyCandidates(
        source_graph=str(graph_path), source_merge_map=str(merge_map_path),
        timestamp=datetime.now(timezone.utc), model=model_name, embed_text=embed_text,
        distance_threshold=distance_threshold,
        n_themes=len(themes),
        n_clusters=len(clusters),
        n_singletons=sum(1 for c in clusters if c.size == 1),
        seed_edges=[SeedEdge(specifico=s, generale=d, confidence=c) for s, d, c in acyclic],
        removed_cycle_edges=[SeedEdge(specifico=s, generale=d, confidence=c) for s, d, c in removed],
        existing_caps=caps, intermediate_nodes=intermediates,
        clusters=clusters,
    )

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "hierarchy_candidates.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8")
        _write_tsv(report, out_dir / "theme_clusters.tsv")

    _print_summary(report)
    return report


def _write_tsv(r: HierarchyCandidates, path: Path) -> None:
    lines = ["\t".join(["cluster_id", "theme_id", "name", "freq", "is_cap", "in_refinement"])]
    cap_set = set(r.existing_caps)
    refine = {n for e in r.seed_edges for n in (e.specifico, e.generale)}
    for c in r.clusters:
        for m in c.members:
            lines.append("\t".join([
                str(c.cluster_id), m.id, m.name, str(m.freq),
                "yes" if m.id in cap_set else "",
                "yes" if m.id in refine else "",
            ]))
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_summary(r: HierarchyCandidates) -> None:
    print(f"stage 5-3a — candidati gerarchia  (modello {r.model}, embed={r.embed_text}, "
          f"soglia distanza={r.distance_threshold})")
    print(f"  Theme totali     : {r.n_themes}")
    print(f"  --- SEED DAG dai refinement ---")
    print(f"  archi seed (aciclici): {len(r.seed_edges)}   cicli rotti: {len(r.removed_cycle_edges)}")
    for e in r.removed_cycle_edges:
        print(f"    [rotto] {e.specifico} -> {e.generale} (conf {e.confidence})")
    print(f"  cappelli esistenti ({len(r.existing_caps)}): {r.existing_caps}")
    print(f"  intermedi ({len(r.intermediate_nodes)}): {r.intermediate_nodes}")
    print(f"  --- CLUSTERING ---")
    print(f"  cluster: {r.n_clusters}   (di cui singoletti: {r.n_singletons})")
    # istogramma dimensioni
    sizes = [c.size for c in r.clusters]
    buckets = {"1": 0, "2-3": 0, "4-6": 0, "7-12": 0, "13+": 0}
    for s in sizes:
        k = "1" if s == 1 else "2-3" if s <= 3 else "4-6" if s <= 6 else "7-12" if s <= 12 else "13+"
        buckets[k] += 1
    print("  dimensioni cluster:", "  ".join(f"{k}:{v}" for k, v in buckets.items()))
    print("  cluster più grandi (size, cohesion, cap esistente, primi membri):")
    for c in r.clusters[:12]:
        names = ", ".join(m.name for m in c.members[:5])
        cap = f" cap={c.existing_caps}" if c.existing_caps else ""
        more = " ..." if c.size > 5 else ""
        print(f"    #{c.cluster_id} n={c.size} coh={c.cohesion:.2f}{cap}  [{names}{more}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-3a: candidati per la gerarchia tematica.")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="enriched_graph.json (5-2c)")
    ap.add_argument("--merge-map", type=Path, default=DEFAULT_MERGE_MAP, help="theme_merge_map.json (5-2c)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="cartella di output")
    ap.add_argument("--embed", choices=["name", "name_desc"], default="name")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="modello di embedding (path locale o nome HF)")
    ap.add_argument("--distance-threshold", type=float, default=DEFAULT_DISTANCE,
                    help="soglia 1-coseno del clustering (default 0.45; più bassa = cluster più stretti)")
    ap.add_argument("--device", default=None, help="es. 'cuda', 'cpu' (default auto)")
    ap.add_argument("--dry-run", action="store_true", help="non scrive file, stampa solo il riepilogo")
    args = ap.parse_args()

    run(args.graph, args.merge_map, args.out,
        embed_text=args.embed, model_name=args.model,
        distance_threshold=args.distance_threshold, device=args.device, dry_run=args.dry_run)


if __name__ == "__main__":
    main()