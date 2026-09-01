# src/stage_5-6_health_checkup.py
"""
================================================================================
STADIO 5 — FASE 6: HEALTH CHECKUP (checkpoint di fine enrich)
================================================================================

COS'È
  Strumento di **convalida** che lo stadio 5 (enrich) sia riuscito e il grafo
  arricchito sia pronto per lo stadio 6 (index). Deterministico, zero LLM.
  Stesso contratto del `stage_4-5_health_checkup.py` (ADR-022), adattato
  all'enrich.

DIFFERENZA CHIAVE RISPETTO ALLO STADIO 4
  Nello stadio 4 un `review_needed=True` BLOCCA (decisione di resolution non
  chiusa). Nello stadio 5 il `review_needed=True` sugli artefatti sintetici
  (EMBODIES, ECHOES, TRANSFORMS_INTO, cappelli sintetizzati) è ATTESO e VOLUTO:
  è il modo con cui l'enrich segnala "questa inferenza va vista da occhio umano
  PRIMA dell'indice". Quindi NON blocca: viene raccolto nella review_queue. Un
  `review_needed=True` su un elemento NON di origine enrich, invece, è un residuo
  anomalo (warn).

COME SI USA
  python src/stage_5-6_health_checkup.py
  → apri `data/stage_5/6_health_checkup/dashboard.html` nel browser

INPUT (default)
  data/stage_5/5_transforms/enriched_graph.json   ← grafo finale dell'enrich
  + mappe di decisione dei sotto-stadi (lette se presenti, opzionali):
    1_embodies/embodies_map.json, 2_themes/theme_merge_map.json,
    3_hierarchy/hierarchy_map.json, 4_echoes/echoes_map.json,
    5_transforms/transforms_map.json

OUTPUT (cartella data/stage_5/6_health_checkup/)
  dashboard.html   ← INIZIA DA QUI: guida visiva, verdetto, check, campioni
  checks.json      esito di ogni controllo (pass / warn / fail / info)
  metrics.json     numeri grezzi (conteggi, ratio, archi di enrich)
  review_queue.json item che richiedono occhio umano (artefatti enrich + hub)
  health_log.json  versioni, parametri, verdetto finale

VERDETTO FINALE (health_log.verdict)
  pass               stadio 5 OK, si può procedere all'index
  pass_with_warnings stadio 5 OK, ma leggere i warn (review consigliata)
  fail               bloccare: correggere l'enrich prima dell'index

Vedi ADR-022, ADR-024/025/026/027, PIPELINE.md sezione "Pattern health_checkup".
================================================================================
"""

from __future__ import annotations

import html
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.deduplication_schema import ResolvedGraph
from src.schema import EdgeType, NodeType

_STAGE_5 = PROJECT_ROOT / "data" / "stage_5"
INPUT_GRAPH = _STAGE_5 / "5_transforms" / "enriched_graph.json"
OUT_DIR = _STAGE_5 / "6_health_checkup"

# Mappe di decisione dei sotto-stadi (opzionali: lette se presenti).
MAP_EMBODIES = _STAGE_5 / "1_embodies" / "embodies_map.json"
MAP_THEME_MERGE = _STAGE_5 / "2_themes" / "theme_merge_map.json"
MAP_HIERARCHY = _STAGE_5 / "3_hierarchy" / "hierarchy_map.json"
MAP_ECHOES = _STAGE_5 / "4_echoes" / "echoes_map.json"
MAP_TRANSFORMS = _STAGE_5 / "5_transforms" / "transforms_map.json"

STAGE_VERSION = "0.1.0"
TOP_HUB_N = 10
HIGH_PROVENANCE_N = 20
MAX_SAMPLES = 8

# Archi prodotti esclusivamente dall'enrich (stadio 5).
ENRICH_EDGE_TYPES = (
    EdgeType.EMBODIES,
    EdgeType.SPECIALIZES,
    EdgeType.ECHOES,
    EdgeType.TRANSFORMS_INTO,
)
# Sentinelle di provenienza (model) usate dai sotto-stadi dell'enrich.
ENRICH_MODEL_PREFIX = "stage_5"
# Prefisso dei merged_from sintetici dell'enrich.
ENRICH_MERGED_PREFIX = "stage5_"

CheckStatus = Literal["pass", "warn", "fail", "info", "skip"]
Verdict = Literal["pass", "pass_with_warnings", "fail"]


# -----------------------------------------------------------------------------
# Catalogo controlli — guida rapida incorporata nel codice
# -----------------------------------------------------------------------------
# blocks_stage=True → se status=fail, il verdetto finale è fail.
# -----------------------------------------------------------------------------

CHECK_CATALOG: dict[str, dict[str, Any]] = {
    "graph_integrity": {
        "title": "Integrità grafo arricchito",
        "category": "struttura",
        "blocks_stage": True,
        "why": "Il grafo deve validare Pydantic (ResolvedGraph): integrità referenziale, "
               "EDGE_COMPATIBILITY (incluso SPECIALIZES, schema 0.3.0), tipi degli estremi allineati.",
        "what_to_check": "Lo script carica enriched_graph.json senza errori Pydantic.",
        "solutions": [
            "Rilanciare il sotto-stadio enrich che ha scritto il grafo.",
            "Verificare il bump schema (SPECIALIZES, is_macro) richiesto da ADR-023.",
        ],
    },
    "enrich_arcs_materialized": {
        "title": "Archi di arricchimento presenti",
        "category": "enrich",
        "blocks_stage": False,
        "why": "L'enrich deve aver aggiunto archi cross-chunk: EMBODIES, SPECIALIZES, ECHOES, TRANSFORMS_INTO.",
        "what_to_check": "Conteggio per tipo > 0. Se tutti a zero, l'enrich non ha prodotto nulla.",
        "solutions": [
            "Verificare che i sotto-stadi 5-1…5-5 siano stati eseguiti in catena.",
            "Controllare le mappe di decisione (embodies_map, hierarchy_map, echoes_map, transforms_map).",
        ],
    },
    "specializes_acyclic": {
        "title": "Gerarchia tematica aciclica",
        "category": "enrich",
        "blocks_stage": True,
        "why": "Gli archi SPECIALIZES (specifico→generale) devono formare un DAG: un ciclo è una "
               "contraddizione topologica (A più specifico di B e viceversa).",
        "what_to_check": "Nessun ciclo nel sottografo dei soli SPECIALIZES.",
        "solutions": [
            "Il 5-3a e il 5-3d rompono i cicli per costruzione: un ciclo qui è un bug.",
            "Rilanciare stage_5-3d_hierarchy_build (rottura cicli su grafo combinato).",
        ],
    },
    "macro_theme_consistency": {
        "title": "Coerenza macro-temi (cappelli)",
        "category": "enrich",
        "blocks_stage": False,
        "why": "Un Theme con is_macro=True è un cappello: dovrebbe raccogliere ≥1 sotto-tema "
               "(arco SPECIALIZES entrante). Un cappello senza figli è un artefatto inutile.",
        "what_to_check": "Ogni nodo is_macro ha almeno un SPECIALIZES entrante.",
        "solutions": [
            "Ispezionare hierarchy_map.json (cappelli promossi/sintetizzati).",
            "Se cappello vuoto: candidato a rimozione o ri-aggancio nel 5-3.",
        ],
    },
    "enrich_review_queue": {
        "title": "Artefatti enrich da rivedere",
        "category": "review umana",
        "blocks_stage": False,
        "why": "EMBODIES, ECHOES, TRANSFORMS_INTO e i cappelli sintetizzati portano review_needed=True "
               "di proposito: sono inferenze da confermare a occhio PRIMA dell'indice. Atteso, non blocca.",
        "what_to_check": "Numero di artefatti enrich in review_queue; campionare reasoning/evidence_span.",
        "solutions": [
            "Review umana in chat dedicata usando i chunk_ids della review_queue.",
            "Confermati → settare human_validated=True; scartati → rimuovere e rigenerare.",
        ],
    },
    "unexpected_review_needed": {
        "title": "review_needed non di origine enrich",
        "category": "struttura",
        "blocks_stage": False,
        "why": "Lo stadio 4 era pass solo con zero review_needed. Un review_needed su un elemento "
               "NON prodotto dall'enrich è un residuo anomalo trascinato a valle.",
        "what_to_check": "Tutti gli elementi review_needed sono di origine enrich (provenance stage_5* o "
                         "tipo arco di enrich).",
        "solutions": [
            "Risalire all'elemento e capire da quale stadio arriva.",
            "Se è residuo di stadio 4: correggere a monte e rieseguire l'enrich.",
        ],
    },
    "theme_consolidation_pending": {
        "title": "Decisioni Theme ancora aperte (5-2c)",
        "category": "enrich",
        "blocks_stage": False,
        "why": "Il 5-2c lascia in sospeso i cluster sotto soglia (review_clusters) e i conflitti "
               "same×refinement (conflict_clusters): decisioni umane non chiuse.",
        "what_to_check": "review_clusters + conflict_clusters in theme_merge_map.json.",
        "solutions": [
            "Confermare/scartare i review_clusters a mano (flag applied + rilancio 5-2c --update).",
            "Decidere la promozione del conflict cluster (es. anima/corpo).",
        ],
    },
    "embodies_skipped": {
        "title": "Proposte EMBODIES saltate",
        "category": "enrich",
        "blocks_stage": False,
        "why": "EMBODIES ammette solo (Event|Person)→Theme: le proposte Phase→Theme dello stadio 4 "
               "vengono saltate (punto aperto ADR-023/027).",
        "what_to_check": "skipped_incompatible in embodies_map.json.",
        "solutions": [
            "Se il pattern Phase→Theme ricorre: valutare estensione EDGE_COMPATIBILITY[EMBODIES] (bump schema).",
            "Altrimenti accettare: è rumore residuo, non blocca.",
        ],
    },
    "hub_spot_check": {
        "title": "Hub ad alta reuse (spot-check)",
        "category": "review umana",
        "blocks_stage": False,
        "why": "Nodi con molte provenances (adriano, ere, macro-temi) sono centrali: errori qui si propagano.",
        "what_to_check": "Campionare 2–3 chunk per hub; name/description coerenti?",
        "solutions": [
            "Review umana usando i chunk_ids in review_queue.",
            "Per i macro-temi: il nome del cappello è generale e corretto?",
        ],
    },
    "human_validation_coverage": {
        "title": "Copertura human_validated",
        "category": "review umana",
        "blocks_stage": False,
        "why": "Post-enrich è il momento previsto per la review umana sul grafo completo (ADR-022).",
        "what_to_check": "nodes_fully_validated / nodes_total (atteso ~0% finché la review non parte).",
        "solutions": [
            "Pianificare la review post-enrich prima dell'indice (stadio 6).",
            "Non bloccare lo stadio 5 per human_validated=false.",
        ],
    },
}


# -----------------------------------------------------------------------------
# IO
# -----------------------------------------------------------------------------

def _load_graph(path: Path) -> ResolvedGraph:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ResolvedGraph.model_validate(raw)


def _load_map(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    if f == c:
        return round(sorted_v[f], 4)
    return round(sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f), 4)


# -----------------------------------------------------------------------------
# Origine enrich
# -----------------------------------------------------------------------------

def _node_is_enrich(node) -> bool:
    return any((p.model or "").startswith(ENRICH_MODEL_PREFIX) for p in node.provenances)


def _edge_is_enrich(edge) -> bool:
    """Arco PRODOTTO dall'enrich. Basato su provenienza/merged_from, NON sul tipo:
    EMBODIES ed ECHOES esistono già nell'estrazione (stadio 3), quindi il tipo da
    solo sovrastimerebbe il contributo dell'enrich."""
    if any(mf.startswith(ENRICH_MERGED_PREFIX) for mf in edge.merged_from):
        return True
    return any((p.model or "").startswith(ENRICH_MODEL_PREFIX) for p in edge.provenances)


# -----------------------------------------------------------------------------
# Campioni
# -----------------------------------------------------------------------------

def _node_chunk_ids(node) -> list[str]:
    return sorted({p.chunk_id for p in node.provenances})


def _sample_node(node) -> dict[str, Any]:
    return {
        "kind": "node",
        "id": node.id,
        "type": node.type.value,
        "name": node.name,
        "is_macro": getattr(node, "is_macro", False),
        "n_provenances": len(node.provenances),
        "chunk_ids": _node_chunk_ids(node)[:5],
        "review_reason": node.review_reason,
    }


def _sample_edge(edge) -> dict[str, Any]:
    return {
        "kind": "edge",
        "id": f"{edge.source_id}|{edge.type.value}|{edge.target_id}",
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "type": edge.type.value,
        "chunk_ids": sorted({p.chunk_id for p in edge.provenances})[:5],
        "review_reason": edge.review_reason,
        "description": (edge.description or "")[:120],
    }


# -----------------------------------------------------------------------------
# Cicli sul sottografo SPECIALIZES
# -----------------------------------------------------------------------------

def _find_cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    """Ritorna un ciclo (lista di nodi) sul grafo diretto, o None se aciclico."""
    adj: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for s, t in edges:
        adj[s].append(t)
        nodes.add(s)
        nodes.add(t)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for v in adj[u]:
            if color[v] == GRAY:
                idx = stack.index(v)
                return stack[idx:] + [v]
            if color[v] == WHITE:
                res = dfs(v)
                if res:
                    return res
        stack.pop()
        color[u] = BLACK
        return None

    for n in nodes:
        if color[n] == WHITE:
            res = dfs(n)
            if res:
                return res
    return None


# -----------------------------------------------------------------------------
# Metriche
# -----------------------------------------------------------------------------

def compute_metrics(graph: ResolvedGraph, maps: dict[str, dict | None]) -> dict[str, Any]:
    node_types = Counter(n.type.value for n in graph.nodes)
    edge_types = Counter(e.type.value for e in graph.edges)

    # contributo dell'enrich: archi EFFETTIVAMENTE aggiunti dallo stadio 5,
    # contati per provenienza (non per tipo), raggruppati per tipo di arco.
    enrich_contribution: dict[str, int] = defaultdict(int)
    for e in graph.edges:
        if _edge_is_enrich(e):
            enrich_contribution[e.type.value] += 1
    enrich_contribution = dict(enrich_contribution)
    enrich_contribution_total = sum(enrich_contribution.values())

    # macro-temi e gerarchia
    macro_nodes = [n for n in graph.nodes if getattr(n, "is_macro", False)]
    specializes = [(e.source_id, e.target_id) for e in graph.edges if e.type == EdgeType.SPECIALIZES]
    incoming_spec: dict[str, int] = defaultdict(int)
    for _, t in specializes:
        incoming_spec[t] += 1
    macro_without_children = [n.id for n in macro_nodes if incoming_spec.get(n.id, 0) == 0]
    cycle = _find_cycle(specializes)

    # review_needed: separazione enrich vs anomalo
    review_nodes_enrich, review_nodes_other = [], []
    for n in graph.nodes:
        if n.review_needed:
            (review_nodes_enrich if _node_is_enrich(n) else review_nodes_other).append(n.id)
    review_edges_enrich, review_edges_other = [], []
    for e in graph.edges:
        if e.review_needed:
            key = f"{e.source_id}|{e.type.value}|{e.target_id}"
            (review_edges_enrich if _edge_is_enrich(e) else review_edges_other).append(key)

    # confidence aggregata
    confidences: list[float] = []
    for n in graph.nodes:
        for p in n.provenances:
            if p.confidence is not None:
                confidences.append(p.confidence)
    for e in graph.edges:
        for p in e.provenances:
            if p.confidence is not None:
                confidences.append(p.confidence)

    prov_per_node = [len(n.provenances) for n in graph.nodes]

    # mappe dei sotto-stadi (graceful se assenti)
    tm = maps.get("theme_merge") or {}
    em = maps.get("embodies") or {}
    hm = maps.get("hierarchy") or {}

    return {
        "nodes_total": len(graph.nodes),
        "edges_total": len(graph.edges),
        "nodes_by_type": dict(node_types),
        "edges_by_type": dict(edge_types),
        "enrich_contribution": enrich_contribution,
        "enrich_contribution_total": enrich_contribution_total,
        "hierarchy": {
            "macro_themes": len(macro_nodes),
            "caps_promoted": hm.get("n_caps_promoted"),
            "caps_synthesized": hm.get("n_caps_synthesized"),
            "specializes": len(specializes),
            "macro_without_children": macro_without_children,
            "has_cycle": cycle is not None,
            "cycle_sample": cycle,
        },
        "theme_consolidation": {
            "applied_clusters": len(tm.get("applied_clusters", [])),
            "review_clusters": len(tm.get("review_clusters", [])),
            "conflict_clusters": len(tm.get("conflict_clusters", [])),
            "n_same": tm.get("n_same"),
            "n_refinement": tm.get("n_refinement"),
        },
        "embodies": {
            "materialized": em.get("materialized"),
            "skipped_incompatible": em.get("skipped_incompatible"),
        },
        "review_needed": {
            "nodes_enrich": len(review_nodes_enrich),
            "nodes_other": len(review_nodes_other),
            "edges_enrich": len(review_edges_enrich),
            "edges_other": len(review_edges_other),
        },
        "confidence": {
            "n_samples": len(confidences),
            "min": round(min(confidences), 4) if confidences else None,
            "median": round(statistics.median(confidences), 4) if confidences else None,
            "max": round(max(confidences), 4) if confidences else None,
        },
        "provenances_per_node": {
            "median": int(statistics.median(prov_per_node)) if prov_per_node else 0,
            "p95": _percentile([float(x) for x in prov_per_node], 95),
            "max": max(prov_per_node) if prov_per_node else 0,
        },
        "human_validated": {
            "nodes_fully_validated": sum(
                1 for n in graph.nodes if n.provenances and all(p.human_validated for p in n.provenances)
            ),
            "nodes_total": len(graph.nodes),
        },
        "_review_nodes_other_ids": review_nodes_other,
        "_review_edges_other_ids": review_edges_other,
        "_macro_without_children": macro_without_children,
    }


# -----------------------------------------------------------------------------
# Controlli
# -----------------------------------------------------------------------------

def _check_result(
    check_id: str,
    status: CheckStatus,
    *,
    count: int = 0,
    value: Any = None,
    target: str = "",
    message: str = "",
    samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    meta = CHECK_CATALOG[check_id]
    return {
        "check_id": check_id,
        "title": meta["title"],
        "category": meta["category"],
        "status": status,
        "blocks_stage": meta["blocks_stage"],
        "count": count,
        "value": value,
        "target": target,
        "message": message,
        "why": meta["why"],
        "what_to_check": meta["what_to_check"],
        "solutions": meta["solutions"],
        "samples": samples or [],
    }


def run_checks(graph: ResolvedGraph, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    node_by_id = {n.id: n for n in graph.nodes}

    # 1) graph_integrity — siamo qui → caricato e validato
    results.append(_check_result(
        "graph_integrity", "pass",
        message="ResolvedGraph caricato e validato (referential integrity + EDGE_COMPATIBILITY).",
    ))

    # 2) enrich_arcs_materialized
    ee = metrics["enrich_contribution"]
    ee_total = metrics["enrich_contribution_total"]
    ee_msg = "  ".join(f"{k}={v}" for k, v in sorted(ee.items())) or "nessuno"
    if ee_total == 0:
        results.append(_check_result(
            "enrich_arcs_materialized", "warn",
            count=0, message="Nessun arco aggiunto dall'enrich (contributo nullo).",
        ))
    else:
        results.append(_check_result(
            "enrich_arcs_materialized", "info",
            count=ee_total, message=f"{ee_total} archi aggiunti dall'enrich: {ee_msg}.",
        ))

    # 3) specializes_acyclic
    h = metrics["hierarchy"]
    if h["specializes"] == 0:
        results.append(_check_result(
            "specializes_acyclic", "skip",
            message="Nessun arco SPECIALIZES nel grafo.",
        ))
    elif h["has_cycle"]:
        results.append(_check_result(
            "specializes_acyclic", "fail",
            value=" → ".join(h["cycle_sample"] or []),
            message="Ciclo nel sottografo SPECIALIZES: gerarchia tematica non aciclica.",
        ))
    else:
        results.append(_check_result(
            "specializes_acyclic", "pass",
            count=h["specializes"],
            message=f"{h['specializes']} archi SPECIALIZES, nessun ciclo (DAG valido).",
        ))

    # 4) macro_theme_consistency
    mwc = metrics["_macro_without_children"]
    if h["macro_themes"] == 0:
        results.append(_check_result(
            "macro_theme_consistency", "skip",
            message="Nessun macro-tema (is_macro) nel grafo.",
        ))
    elif mwc:
        samples = [_sample_node(node_by_id[i]) for i in mwc[:MAX_SAMPLES] if i in node_by_id]
        results.append(_check_result(
            "macro_theme_consistency", "warn",
            count=len(mwc),
            message=f"{len(mwc)} macro-temi senza SPECIALIZES entrante (cappelli vuoti) "
                    f"su {h['macro_themes']} totali.",
            samples=samples,
        ))
    else:
        results.append(_check_result(
            "macro_theme_consistency", "pass",
            count=h["macro_themes"],
            message=f"Tutti i {h['macro_themes']} macro-temi raccolgono almeno un sotto-tema.",
        ))

    # 5) enrich_review_queue (atteso, info)
    rn = metrics["review_needed"]
    enrich_review = rn["nodes_enrich"] + rn["edges_enrich"]
    samples = []
    for n in graph.nodes:
        if n.review_needed and _node_is_enrich(n):
            samples.append(_sample_node(n))
        if len(samples) >= MAX_SAMPLES:
            break
    for e in graph.edges:
        if len(samples) >= MAX_SAMPLES:
            break
        if e.review_needed and _edge_is_enrich(e):
            samples.append(_sample_edge(e))
    results.append(_check_result(
        "enrich_review_queue", "info",
        count=enrich_review,
        message=f"{enrich_review} artefatti enrich con review_needed=True (atteso): "
                f"{rn['nodes_enrich']} nodi (cappelli sintetizzati), {rn['edges_enrich']} archi "
                f"(EMBODIES/ECHOES/TRANSFORMS_INTO).",
        samples=samples,
    ))

    # 6) unexpected_review_needed
    other = rn["nodes_other"] + rn["edges_other"]
    if other == 0:
        results.append(_check_result(
            "unexpected_review_needed", "pass",
            message="Tutti i review_needed sono di origine enrich (nessun residuo anomalo).",
        ))
    else:
        samples = [_sample_node(node_by_id[i]) for i in metrics["_review_nodes_other_ids"][:MAX_SAMPLES] if i in node_by_id]
        results.append(_check_result(
            "unexpected_review_needed", "warn",
            count=other,
            message=f"{other} elementi review_needed NON di origine enrich (residuo da monte).",
            samples=samples,
        ))

    # 7) theme_consolidation_pending
    tc = metrics["theme_consolidation"]
    pending = (tc["review_clusters"] or 0) + (tc["conflict_clusters"] or 0)
    if tc["review_clusters"] is None and tc["conflict_clusters"] is None:
        results.append(_check_result(
            "theme_consolidation_pending", "skip",
            message="theme_merge_map.json assente: impossibile valutare le decisioni Theme.",
        ))
    elif pending == 0:
        results.append(_check_result(
            "theme_consolidation_pending", "pass",
            message="Nessun cluster Theme in review o in conflitto.",
        ))
    else:
        results.append(_check_result(
            "theme_consolidation_pending", "warn",
            count=pending,
            message=f"{tc['review_clusters']} cluster in review + {tc['conflict_clusters']} in conflitto "
                    f"(same×refinement) ancora da decidere.",
        ))

    # 8) embodies_skipped
    em = metrics["embodies"]
    if em["skipped_incompatible"] is None:
        results.append(_check_result(
            "embodies_skipped", "skip",
            message="embodies_map.json assente.",
        ))
    elif em["skipped_incompatible"]:
        results.append(_check_result(
            "embodies_skipped", "info",
            count=em["skipped_incompatible"],
            message=f"{em['skipped_incompatible']} proposte EMBODIES saltate "
                    f"(Phase→Theme non ammesso) — punto aperto noto.",
        ))
    else:
        results.append(_check_result(
            "embodies_skipped", "pass",
            message="Nessuna proposta EMBODIES saltata.",
        ))

    # 9) hub_spot_check
    hubs = [n for n in graph.nodes if len(n.provenances) >= HIGH_PROVENANCE_N]
    hubs.sort(key=lambda n: len(n.provenances), reverse=True)
    if hubs:
        results.append(_check_result(
            "hub_spot_check", "warn",
            count=len(hubs),
            message=f"{len(hubs)} hub con ≥ {HIGH_PROVENANCE_N} provenances — spot-check consigliato.",
            samples=[_sample_node(n) for n in hubs[:TOP_HUB_N]],
        ))
    else:
        results.append(_check_result(
            "hub_spot_check", "pass",
            message="Nessun hub oltre soglia provenance.",
        ))

    # 10) human_validation_coverage
    hv = metrics["human_validated"]
    pct_hv = round(100 * hv["nodes_fully_validated"] / hv["nodes_total"], 2) if hv["nodes_total"] else 0
    results.append(_check_result(
        "human_validation_coverage", "info",
        value=pct_hv,
        message=f"{hv['nodes_fully_validated']}/{hv['nodes_total']} nodi fully human_validated "
                f"({pct_hv}%) — la review post-enrich è il passo previsto ora.",
    ))

    return results


def build_review_queue(graph: ResolvedGraph, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Artefatti enrich da confermare + hub da spot-check."""
    items: list[dict[str, Any]] = []

    for n in graph.nodes:
        if n.review_needed:
            items.append({
                "priority": 1,
                "kind": "node",
                "entity_id": n.id,
                "entity_type": n.type.value,
                "origin": "enrich" if _node_is_enrich(n) else "other",
                "reason": n.review_reason,
                "chunk_ids": _node_chunk_ids(n),
            })
    for e in graph.edges:
        if e.review_needed:
            items.append({
                "priority": 1,
                "kind": "edge",
                "entity_id": f"{e.source_id}|{e.type.value}|{e.target_id}",
                "entity_type": e.type.value,
                "origin": "enrich" if _edge_is_enrich(e) else "other",
                "reason": e.review_reason,
                "chunk_ids": sorted({p.chunk_id for p in e.provenances}),
            })

    hub_check = next((c for c in checks if c["check_id"] == "hub_spot_check"), None)
    if hub_check and hub_check["samples"]:
        for s in hub_check["samples"]:
            items.append({
                "priority": 2,
                "kind": "node",
                "entity_id": s["id"],
                "entity_type": s["type"],
                "origin": "hub",
                "reason": "hub_spot_check",
                "chunk_ids": s.get("chunk_ids", []),
            })

    items.sort(key=lambda x: (x["priority"], x["entity_id"]))
    return {"total": len(items), "items": items}


def compute_verdict(checks: list[dict[str, Any]]) -> dict[str, Any]:
    fails = [c for c in checks if c["status"] == "fail" and c["blocks_stage"]]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        verdict: Verdict = "fail"
        headline = "Stadio 5 NON convalidato — correggere l'enrich prima dell'index."
    elif warns:
        verdict = "pass_with_warnings"
        headline = "Stadio 5 convalidato con avvisi — review consigliata, si può procedere."
    else:
        verdict = "pass"
        headline = "Stadio 5 convalidato — pronto per stadio 6 index."
    return {
        "verdict": verdict,
        "headline": headline,
        "fail_count": len(fails),
        "warn_count": len(warns),
        "blocking_checks": [c["check_id"] for c in fails],
        "warning_checks": [c["check_id"] for c in warns],
    }


# -----------------------------------------------------------------------------
# Dashboard HTML
# -----------------------------------------------------------------------------

def _status_badge(status: str) -> str:
    colors = {
        "pass": ("#0d7a4a", "#e6f4ed"),
        "warn": ("#9a6700", "#fff8e6"),
        "fail": ("#b42318", "#fdecea"),
        "info": ("#175cd3", "#eff4ff"),
        "skip": ("#667085", "#f2f4f7"),
    }
    fg, bg = colors.get(status, ("#333", "#eee"))
    return (
        f'<span class="badge" style="color:{fg};background:{bg};border:1px solid {fg}22">'
        f"{status.upper()}</span>"
    )


def _verdict_banner(verdict: dict[str, Any]) -> str:
    colors = {
        "pass": ("#0d7a4a", "#e6f4ed"),
        "pass_with_warnings": ("#9a6700", "#fff8e6"),
        "fail": ("#b42318", "#fdecea"),
    }
    fg, bg = colors.get(verdict["verdict"], ("#333", "#eee"))
    label = verdict["verdict"].replace("_", " ").upper()
    return f"""
    <div class="verdict" style="background:{bg};border-left:4px solid {fg}">
      <div class="verdict-label" style="color:{fg}">{label}</div>
      <div class="verdict-headline">{html.escape(verdict['headline'])}</div>
      <div class="verdict-meta">
        fail blocanti: {verdict['fail_count']} · warn: {verdict['warn_count']}
      </div>
    </div>"""


def render_dashboard(
    *,
    metrics: dict[str, Any],
    checks: list[dict[str, Any]],
    verdict: dict[str, Any],
    health_log: dict[str, Any],
    review_queue: dict[str, Any],
) -> str:
    m = metrics
    et = m["edges_by_type"]
    h = m["hierarchy"]
    cards = [
        ("Nodi", m["nodes_total"]),
        ("Archi", m["edges_total"]),
        ("Macro-temi", h["macro_themes"]),
        ("SPECIALIZES", et.get("SPECIALIZES", 0)),
        ("ECHOES (tot)", et.get("ECHOES", 0)),
        ("TRANSFORMS_INTO", et.get("TRANSFORMS_INTO", 0)),
        ("Archi da enrich", m["enrich_contribution_total"]),
        ("Review queue", review_queue["total"]),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="card-n">{html.escape(str(v))}</div>'
        f'<div class="card-l">{html.escape(k)}</div></div>'
        for k, v in cards
    )

    check_rows = []
    for c in checks:
        samples_html = ""
        if c["samples"]:
            lines = []
            for s in c["samples"][:5]:
                if s.get("kind") == "edge":
                    lines.append(
                        f"<li><code>{html.escape(s['source_id'])}</code> "
                        f"-{html.escape(s['type'])}-> <code>{html.escape(s['target_id'])}</code></li>"
                    )
                elif "name" in s:
                    macro = " ⬆macro" if s.get("is_macro") else ""
                    lines.append(f"<li><code>{html.escape(s['id'])}</code> — {html.escape(s.get('name',''))}{macro}</li>")
                else:
                    lines.append(f"<li><code>{html.escape(str(s.get('id', s)))}</code></li>")
            samples_html = f"<ul class='samples'>{''.join(lines)}</ul>"

        sol_html = "<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in c["solutions"]) + "</ul>"

        check_rows.append(f"""
        <tr class="check-row status-{c['status']}">
          <td>{_status_badge(c['status'])}</td>
          <td><strong>{html.escape(c['title'])}</strong><br>
              <span class="muted">{html.escape(c['check_id'])} · {html.escape(c['category'])}</span>
              {"<br><span class='block'>BLOCCA STADIO</span>" if c['blocks_stage'] else ""}
          </td>
          <td>{html.escape(c['message'])}<br>
              {f"<span class='muted'>valore: {html.escape(str(c['value']))} · target: {html.escape(c['target'])}</span>" if c.get('value') is not None or c.get('target') else ""}
              {f"<span class='muted'> · count: {c['count']}</span>" if c.get('count') else ""}
          </td>
          <td class="why">{html.escape(c['why'])}</td>
          <td class="actions">
            <p><strong>Cosa verificare</strong></p>
            <p>{html.escape(c['what_to_check'])}</p>
            <p><strong>Soluzioni</strong></p>
            {sol_html}
            {samples_html}
          </td>
        </tr>""")

    guide_items = "".join(
        f"<li><strong>{html.escape(v['title'])}</strong> ({k}): {html.escape(v['why'][:120])}…</li>"
        for k, v in CHECK_CATALOG.items()
    )

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>Health checkup — Stadio 5 Enrich</title>
  <style>
    :root {{ font-family: "Segoe UI", system-ui, sans-serif; color: #1a1a1a; line-height: 1.45; }}
    body {{ max-width: 1200px; margin: 0 auto; padding: 24px; background: #fafafa; }}
    h1 {{ margin: 0 0 8px; font-size: 1.6rem; }}
    h2 {{ margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
    .muted {{ color: #667085; font-size: 0.9rem; }}
    .verdict {{ padding: 16px 20px; border-radius: 8px; margin: 20px 0; }}
    .verdict-label {{ font-weight: 700; font-size: 1.1rem; }}
    .verdict-headline {{ font-size: 1.05rem; margin-top: 4px; }}
    .verdict-meta {{ font-size: 0.85rem; color: #667085; margin-top: 8px; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }}
    .card {{ background: #fff; border: 1px solid #e4e7ec; border-radius: 8px; padding: 12px 16px; min-width: 100px; }}
    .card-n {{ font-size: 1.4rem; font-weight: 700; }}
    .card-l {{ font-size: 0.8rem; color: #667085; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 0.92rem; }}
    th, td {{ border: 1px solid #e4e7ec; padding: 10px 12px; vertical-align: top; }}
    th {{ background: #f2f4f7; text-align: left; }}
    .check-row.status-fail {{ background: #fffafa; }}
    .check-row.status-warn {{ background: #fffdf5; }}
    .why {{ max-width: 220px; font-size: 0.88rem; }}
    .actions {{ max-width: 320px; font-size: 0.88rem; }}
    .actions ul {{ margin: 4px 0; padding-left: 18px; }}
    .samples {{ margin-top: 8px; font-size: 0.82rem; }}
    .block {{ color: #b42318; font-size: 0.75rem; font-weight: 700; }}
    code {{ background: #f2f4f7; padding: 1px 4px; border-radius: 3px; font-size: 0.85em; }}
    .guide {{ background: #fff; border: 1px solid #e4e7ec; border-radius: 8px; padding: 16px; }}
    .meta {{ font-size: 0.85rem; color: #667085; }}
  </style>
</head>
<body>
  <h1>Health checkup — Stadio 5 Enrich</h1>
  <p class="meta">
    stage_5-6_health_checkup v{STAGE_VERSION} ·
    {html.escape(health_log.get('timestamp', ''))} ·
    input: <code>{html.escape(health_log.get('input_graph', ''))}</code>
  </p>

  {_verdict_banner(verdict)}

  <h2>Metriche rapide</h2>
  <div class="cards">{cards_html}</div>

  <h2>Guida ai controlli</h2>
  <div class="guide">
    <p>Questo checkpoint convalida l'<strong>enrich</strong> (archi cross-chunk e gerarchia tematica).
       A differenza dello stadio 4, qui <strong>review_needed=True sugli artefatti sintetici è ATTESO</strong>:
       è la coda di review umana pre-index, non un errore.</p>
    <ul>{guide_items}</ul>
    <p class="muted">Dettaglio completo nel docstring di <code>src/stage_5-6_health_checkup.py</code>
       e in <code>checks.json</code>.</p>
  </div>

  <h2>Esito controlli</h2>
  <table>
    <thead>
      <tr>
        <th>Status</th>
        <th>Controllo</th>
        <th>Messaggio</th>
        <th>Perché conta</th>
        <th>Verifica / Soluzioni</th>
      </tr>
    </thead>
    <tbody>{"".join(check_rows)}</tbody>
  </table>

  <h2>Review umana ({review_queue['total']} item)</h2>
  <p class="muted">Artefatti enrich (priority 1) + hub (priority 2).
     Usa <code>data/stage_2/chunks.json</code> con i chunk_ids.</p>
  <p>File: <code>review_queue.json</code></p>

  <h2>Artefatti</h2>
  <ul>
    <li><code>dashboard.html</code> — questo report</li>
    <li><code>checks.json</code> — esiti strutturati</li>
    <li><code>metrics.json</code> — numeri grezzi</li>
    <li><code>review_queue.json</code> — coda review</li>
    <li><code>health_log.json</code> — verdetto e metadata</li>
  </ul>
</body>
</html>"""


# -----------------------------------------------------------------------------
# Orchestrazione
# -----------------------------------------------------------------------------

def run(input_path: Path = INPUT_GRAPH, out_dir: Path = OUT_DIR) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    graph = _load_graph(input_path)
    maps = {
        "embodies": _load_map(MAP_EMBODIES),
        "theme_merge": _load_map(MAP_THEME_MERGE),
        "hierarchy": _load_map(MAP_HIERARCHY),
        "echoes": _load_map(MAP_ECHOES),
        "transforms": _load_map(MAP_TRANSFORMS),
    }

    metrics = compute_metrics(graph, maps)
    public_metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}

    checks = run_checks(graph, metrics)
    verdict = compute_verdict(checks)
    review_queue = build_review_queue(graph, checks)

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(p)

    health_log = {
        "stage": "stage_5-6_health_checkup",
        "stage_version": STAGE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_graph": _rel(input_path),
        "source_run": graph.source_run,
        "source_schema_version": graph.source_schema_version,
        "source_prompt_version": graph.source_prompt_version,
        "dedup_schema_version": graph.dedup_schema_version,
        "maps_found": {k: (v is not None) for k, v in maps.items()},
        "parameters": {
            "top_hub_n": TOP_HUB_N,
            "high_provenance_n": HIGH_PROVENANCE_N,
        },
        "verdict": verdict,
        "summary": {
            "nodes": public_metrics["nodes_total"],
            "edges": public_metrics["edges_total"],
            "enrich_contribution_total": public_metrics["enrich_contribution_total"],
            "macro_themes": public_metrics["hierarchy"]["macro_themes"],
            "checks_total": len(checks),
            "review_queue_total": review_queue["total"],
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(public_metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "checks.json").write_text(
        json.dumps({"checks": checks, "verdict": verdict}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "review_queue.json").write_text(
        json.dumps(review_queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "health_log.json").write_text(
        json.dumps(health_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "dashboard.html").write_text(
        render_dashboard(
            metrics=public_metrics,
            checks=checks,
            verdict=verdict,
            health_log=health_log,
            review_queue=review_queue,
        ),
        encoding="utf-8",
    )

    print(f"input:  {input_path}")
    print(f"output: {out_dir}/")
    print(f"  verdetto: {verdict['verdict'].upper()} — {verdict['headline']}")
    print(f"  dashboard: {out_dir / 'dashboard.html'}")
    print(f"  review queue: {review_queue['total']} item")
    return public_metrics, checks, health_log


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Stadio 5-6: health checkup di fine enrich.")
    ap.add_argument("--input", type=Path, default=INPUT_GRAPH, help="enriched_graph.json finale (output 5-5)")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="cartella di output")
    args = ap.parse_args()
    run(args.input, args.out)
