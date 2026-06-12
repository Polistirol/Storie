# src/stage_4-5_health_checkup.py
"""
================================================================================
STADIO 4 — FASE 5: HEALTH CHECKUP (checkpoint di fine resolve)
================================================================================

COS'È
  Strumento di **convalida** che lo stadio 4 (deduplica) sia riuscito e il grafo
  `resolved_graph.json` sia pronto per lo stadio 5 enrich. Deterministico, zero LLM.

COME SI USA
  python src/stage_4-5_health_checkup.py
  → apri `data/stage_4/5_health_checkup/dashboard.html` nel browser

OUTPUT (cartella data/stage_4/5_health_checkup/)
  dashboard.html   ← INIZIA DA QUI: guida visiva, verdetto, check, campioni
  checks.json      esito di ogni controllo (pass / warn / fail / info)
  metrics.json     numeri grezzi (conteggi, ratio)
  review_queue.json solo item che richiedono occhio umano (hub, review_needed)
  health_log.json  versioni, parametri, verdetto finale

VERDETTO FINALE (health_log.verdict)
  pass               stadio 4 OK, si può procedere allo enrich
  pass_with_warnings stadio 4 OK, ma leggere i warn in dashboard (review consigliata)
  fail               bloccare: correggere stadio 4 prima di enrich

STRUTTURA DEL MODULO
  CHECK_CATALOG     dizionario: ogni controllo spiega problema / check / soluzione
  compute_metrics   metriche aggregate sul grafo risolto
  run_checks        valuta ogni voce del catalogo → status + campioni
  compute_verdict   pass | pass_with_warnings | fail
  render_dashboard  HTML autonomo (nessuna dipendenza esterna)

REVIEW UMANA (opzionale, non blocca da sola)
  `review_queue.json` + filtri severity nella dashboard. Usare chunks.json per
  risalire al testo. Aggiornare human_validated sulle provenances se serve.

Vedi ADR-022, PIPELINE.md sezione "Pattern health_checkup".
================================================================================
"""

from __future__ import annotations

import html
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.deduplication_schema import ResolvedGraph
from src.schema import EdgeType, NodeType

INPUT_GRAPH = PROJECT_ROOT / "data" / "stage_4" / "3_resolve" / "resolved_graph.json"
INPUT_LOG = PROJECT_ROOT / "data" / "stage_4" / "3_resolve" / "resolver_log.json"
OUT_DIR = PROJECT_ROOT / "data" / "stage_4" / "5_health_checkup"

STAGE_VERSION = "0.2.0"
LOW_CONFIDENCE_THRESHOLD = 0.7
TOP_HUB_N = 10
HIGH_PROVENANCE_N = 20
MAX_SAMPLES = 8

CheckStatus = Literal["pass", "warn", "fail", "info", "skip"]
Verdict = Literal["pass", "pass_with_warnings", "fail"]

# -----------------------------------------------------------------------------
# Catalogo controlli — guida rapida incorporata nel codice
# -----------------------------------------------------------------------------
# Ogni check_id è documentato: titolo, perché conta, cosa verificare, soluzioni.
# blocks_stage=True → se status=fail, il verdetto finale è fail.
# -----------------------------------------------------------------------------

CHECK_CATALOG: dict[str, dict[str, Any]] = {
    "graph_integrity": {
        "title": "Integrità grafo risolto",
        "category": "struttura",
        "blocks_stage": True,
        "why": "Il grafo deve validare Pydantic (ResolvedGraph) e avere archi coerenti con i nodi.",
        "what_to_check": "Lo script carica resolved_graph.json senza errori Pydantic; "
                         "source_type/target_type presenti e allineati ai nodi.",
        "solutions": [
            "Rilanciare stage_4-3_resolve dopo aver corretto merge_map / split_map.",
            "Verificare ADR-021: source_type/target_type devono essere prodotti dal resolver.",
        ],
    },
    "review_needed_flags": {
        "title": "Flag review_needed su nodi/archi",
        "category": "struttura",
        "blocks_stage": True,
        "why": "review_needed=True segnala decisioni di resolution non chiuse nel grafo.",
        "what_to_check": "Nessun ResolvedNode/ResolvedEdge con review_needed=True.",
        "solutions": [
            "Ispezionare l'item in review_queue.json.",
            "Correggere in resolver o marcare risolto e rigenerare resolved_graph.",
        ],
    },
    "event_era_anchoring": {
        "title": "Event ancorati a Era (schema 0.2.0)",
        "category": "qualità estrazione",
        "blocks_stage": False,
        "why": "Da PROMPT 0.4.0 ogni Event dovrebbe avere DURING→Era per la spina dorsale temporale. "
               "Event meditativi/atemporali possono legittimamente non averla.",
        "what_to_check": "pct_with_during_era ≥ 60% → pass; < 50% → warn forte. "
                         "Campionare Event senza Era: sono riflessioni o sotto-estrazione?",
        "solutions": [
            "Se sotto-estrazione: rivedere prompt/few-shot stadio 3 (non merge stadio 4).",
            "Se atemporali legittimi: accettare warn e procedere.",
            "ECHOES/tempo fine → stadio 5 enrich.",
        ],
        "thresholds": {"pass_pct": 60.0, "warn_pct": 50.0},
    },
    "narrative_caused_ratio": {
        "title": "Rapporto CAUSED / FOLLOWS",
        "category": "qualità narrativa",
        "blocks_stage": False,
        "why": "CAUSED>>FOLLOWS indica grafo narrativo (causalità), non solo cronaca.",
        "what_to_check": "caused_follows_ratio ≥ 0.6 → pass; < 0.4 → warn.",
        "solutions": [
            "Problema di estrazione stadio 3 → prompt 0.4.0 regola CAUSED vs FOLLOWS.",
            "Non si corregge in stadio 4 (non si inventano archi).",
        ],
        "thresholds": {"pass_ratio": 0.6, "warn_ratio": 0.4},
    },
    "low_confidence_provenance": {
        "title": "Provenance a bassa confidence",
        "category": "qualità estrazione",
        "blocks_stage": False,
        "why": "confidence < 0.7 dovrebbe indicare estrazioni dubbie da rivedere a campione.",
        "what_to_check": "Quanti nodi hanno almeno una provenance sotto soglia; campionare chunk.",
        "solutions": [
            "Review umana su review_queue (opzionale).",
            "Few-shot con confidence bassa esplicita (ADR diagnostica).",
        ],
    },
    "hub_spot_check": {
        "title": "Hub ad alta reuse (spot-check)",
        "category": "review umana",
        "blocks_stage": False,
        "why": "Nodi con molte provenances (es. adriano, adultita) sono centrali: errori qui si propagano.",
        "what_to_check": "Campionare 2–3 chunk per hub; name/description coerenti dopo merge?",
        "solutions": [
            "Review umana in chat dedicata usando chunk_ids in review_queue.",
            "Se merge errato: correggere merge_map e rilanciare resolver (raro su hub).",
        ],
    },
    "merged_nodes_audit": {
        "title": "Nodi da merge non banale",
        "category": "deduplica",
        "blocks_stage": False,
        "why": "merge_method diverso da exact/none indica accorpamenti che meritano spot-check.",
        "what_to_check": "merged_from e name/description del nodo canonico.",
        "solutions": [
            "Verificare merge_map.json se il merge è semanticamente corretto.",
            "merge_map.to_review con canonical_id è decisione stadio 4, non stadio 5.",
        ],
    },
    "low_echoes_count": {
        "title": "Pochi ECHOES cross-chunk",
        "category": "atteso post-resolve",
        "blocks_stage": False,
        "why": "L'estrazione mono-chunk non può collegare scene lontane. Conteggio basso è NORMALE qui.",
        "what_to_check": "ECHOES < 50 è info, non fallimento stadio 4.",
        "solutions": [
            "Demandare a stadio 5 enrich (ADR, analisi diagnostica).",
            "Non forzare nel prompt stadio 3.",
        ],
        "thresholds": {"info_below": 50},
    },
    "enrich_proposals_pending": {
        "title": "Proposte EMBODIES per enrich",
        "category": "atteso post-resolve",
        "blocks_stage": False,
        "why": "Split Event/Theme nello stadio 4 genera proposte, non archi. Devono diventare archi in enrich.",
        "what_to_check": "stage6_proposals nel resolver_log (4 voci sul run Adriano).",
        "solutions": [
            "Implementare stadio 5 enrich per materializzare EMBODIES.",
            "Review umana post-enrich sul grafo completo.",
        ],
    },
    "human_validation_coverage": {
        "title": "Copertura human_validated",
        "category": "review umana",
        "blocks_stage": False,
        "why": "A fine stadio 4 di solito nessun nodo è ancora validato da umano (atteso).",
        "what_to_check": "nodes_fully_validated / nodes_total. Review post-enrich raccomandata.",
        "solutions": [
            "Non bloccare stadio 4 per human_validated=false.",
            "Pianificare review dopo enrich (ADR-022).",
        ],
    },
}


def _load_graph(path: Path) -> ResolvedGraph:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ResolvedGraph.model_validate(raw)


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


def _outgoing_by_event(graph: ResolvedGraph) -> dict[str, list[tuple[str, EdgeType]]]:
    out: dict[str, list[tuple[str, EdgeType]]] = {}
    for e in graph.edges:
        if e.source_type == NodeType.EVENT:
            out.setdefault(e.source_id, []).append((e.target_id, e.type))
    return out


def _node_chunk_ids(node) -> list[str]:
    return sorted({p.chunk_id for p in node.provenances})


def _sample_node(node, max_n: int = MAX_SAMPLES) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type.value,
        "name": node.name,
        "n_provenances": len(node.provenances),
        "chunk_ids": _node_chunk_ids(node)[:5],
        "provenance_sample": [
            {
                "chunk_id": p.chunk_id,
                "confidence": p.confidence,
                "evidence_span": (p.evidence_span or "")[:100],
            }
            for p in node.provenances[:3]
        ],
    }


def compute_metrics(graph: ResolvedGraph, resolver_log: dict | None) -> dict[str, Any]:
    node_types = Counter(n.type.value for n in graph.nodes)
    edge_types = Counter(e.type.value for e in graph.edges)

    event_out = _outgoing_by_event(graph)
    type_by_id = {n.id: n.type for n in graph.nodes}
    event_ids = [n.id for n in graph.nodes if n.type == NodeType.EVENT]
    n_events = len(event_ids)
    n_with_during_era = 0
    n_with_during_any = 0
    events_without_era: list[str] = []

    for eid in event_ids:
        outs = event_out.get(eid, [])
        during_targets = [t for t, et in outs if et == EdgeType.DURING]
        if during_targets:
            n_with_during_any += 1
        if any(type_by_id.get(t) == NodeType.ERA for t in during_targets):
            n_with_during_era += 1
        else:
            events_without_era.append(eid)

    caused = edge_types.get("CAUSED", 0)
    follows = edge_types.get("FOLLOWS", 0)
    echoes = edge_types.get("ECHOES", 0)

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
    merged_nodes = sum(1 for n in graph.nodes if n.merge_method not in ("none", "exact"))

    enrich_proposals = len(resolver_log.get("stage6_proposals", [])) if resolver_log else 0

    review_needed_nodes = sum(1 for n in graph.nodes if n.review_needed)
    review_needed_edges = sum(1 for e in graph.edges if e.review_needed)

    low_conf_nodes = [
        n.id for n in graph.nodes
        if any(p.confidence is not None and p.confidence < LOW_CONFIDENCE_THRESHOLD for p in n.provenances)
    ]

    return {
        "nodes_total": len(graph.nodes),
        "edges_total": len(graph.edges),
        "nodes_by_type": dict(node_types),
        "edges_by_type": dict(edge_types),
        "events": {
            "total": n_events,
            "with_during_any": n_with_during_any,
            "with_during_era": n_with_during_era,
            "without_during_era": len(events_without_era),
            "pct_with_during_era": round(100 * n_with_during_era / n_events, 2) if n_events else None,
        },
        "narrative_arcs": {
            "CAUSED": caused,
            "FOLLOWS": follows,
            "ECHOES": echoes,
            "caused_follows_ratio": round(caused / follows, 3) if follows else None,
        },
        "confidence": {
            "n_samples": len(confidences),
            "min": round(min(confidences), 4) if confidences else None,
            "p25": _percentile(confidences, 25),
            "median": round(statistics.median(confidences), 4) if confidences else None,
            "p75": _percentile(confidences, 75),
            "max": round(max(confidences), 4) if confidences else None,
            "below_threshold": sum(1 for c in confidences if c < LOW_CONFIDENCE_THRESHOLD),
            "nodes_with_low_confidence": len(low_conf_nodes),
            "threshold": LOW_CONFIDENCE_THRESHOLD,
        },
        "provenances_per_node": {
            "median": int(statistics.median(prov_per_node)) if prov_per_node else 0,
            "p95": _percentile([float(x) for x in prov_per_node], 95),
            "max": max(prov_per_node) if prov_per_node else 0,
        },
        "merged_nodes_non_trivial": merged_nodes,
        "enrich_proposals_pending": enrich_proposals,
        "review_needed": {
            "nodes": review_needed_nodes,
            "edges": review_needed_edges,
        },
        "human_validated": {
            "nodes_fully_validated": sum(
                1 for n in graph.nodes if n.provenances and all(p.human_validated for p in n.provenances)
            ),
            "nodes_total": len(graph.nodes),
        },
        "_events_without_era_ids": events_without_era,
        "_low_conf_node_ids": low_conf_nodes,
    }


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


def run_checks(
    graph: ResolvedGraph,
    metrics: dict[str, Any],
    resolver_log: dict | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # graph_integrity — siamo qui → pass
    results.append(_check_result(
        "graph_integrity", "pass",
        message="ResolvedGraph caricato e validato.",
    ))

    # review_needed
    rn = metrics["review_needed"]
    rn_total = rn["nodes"] + rn["edges"]
    if rn_total > 0:
        samples = []
        for n in graph.nodes:
            if n.review_needed:
                samples.append({**_sample_node(n), "review_reason": n.review_reason})
        for e in graph.edges:
            if e.review_needed:
                samples.append({
                    "kind": "edge",
                    "id": f"{e.source_id}|{e.type.value}|{e.target_id}",
                    "review_reason": e.review_reason,
                })
        results.append(_check_result(
            "review_needed_flags", "fail",
            count=rn_total, value=rn,
            message=f"{rn_total} elementi con review_needed=True.",
            samples=samples[:MAX_SAMPLES],
        ))
    else:
        results.append(_check_result(
            "review_needed_flags", "pass",
            message="Nessun review_needed nel grafo.",
        ))

    # event era anchoring
    pct = metrics["events"]["pct_with_during_era"]
    th = CHECK_CATALOG["event_era_anchoring"]["thresholds"]
    era_samples = [
        _sample_node(next(n for n in graph.nodes if n.id == eid))
        for eid in metrics["_events_without_era_ids"][:MAX_SAMPLES]
    ]
    if pct is None:
        era_status: CheckStatus = "skip"
        era_msg = "Nessun Event nel grafo."
    elif pct >= th["pass_pct"]:
        era_status = "pass"
        era_msg = f"{pct}% Event con DURING→Era (soglia pass {th['pass_pct']}%)."
    elif pct >= th["warn_pct"]:
        era_status = "warn"
        era_msg = f"{pct}% Event con Era — sotto target {th['pass_pct']}%, sopra minimo {th['warn_pct']}%."
    else:
        era_status = "warn"
        era_msg = f"Solo {pct}% Event con DURING→Era — molti Event senza spina temporale."
    results.append(_check_result(
        "event_era_anchoring", era_status,
        count=metrics["events"]["without_during_era"],
        value=pct, target=f"≥ {th['pass_pct']}%",
        message=era_msg, samples=era_samples,
    ))

    # caused/follows
    ratio = metrics["narrative_arcs"]["caused_follows_ratio"]
    th2 = CHECK_CATALOG["narrative_caused_ratio"]["thresholds"]
    if ratio is None:
        cf_status: CheckStatus = "skip"
        cf_msg = "Nessun FOLLOWS nel grafo."
    elif ratio >= th2["pass_ratio"]:
        cf_status = "pass"
        cf_msg = f"CAUSED/FOLLOWS = {ratio} (≥ {th2['pass_ratio']})."
    elif ratio >= th2["warn_ratio"]:
        cf_status = "warn"
        cf_msg = f"CAUSED/FOLLOWS = {ratio} — sotto target {th2['pass_ratio']}."
    else:
        cf_status = "warn"
        cf_msg = f"CAUSED/FOLLOWS = {ratio} — grafo più cronaca che narrativo."
    results.append(_check_result(
        "narrative_caused_ratio", cf_status,
        value=ratio, target=f"≥ {th2['pass_ratio']}",
        message=cf_msg,
    ))

    # low confidence
    lc = metrics["confidence"]["nodes_with_low_confidence"]
    if lc == 0:
        results.append(_check_result(
            "low_confidence_provenance", "pass",
            message="Nessun nodo con provenance sotto soglia.",
        ))
    else:
        lc_samples = [
            _sample_node(next(n for n in graph.nodes if n.id == nid))
            for nid in metrics["_low_conf_node_ids"][:MAX_SAMPLES]
        ]
        results.append(_check_result(
            "low_confidence_provenance", "warn",
            count=lc, target=f"confidence < {LOW_CONFIDENCE_THRESHOLD}",
            message=f"{lc} nodi con almeno una provenance sotto {LOW_CONFIDENCE_THRESHOLD}.",
            samples=lc_samples,
        ))

    # hubs
    hubs = [n for n in graph.nodes if len(n.provenances) >= HIGH_PROVENANCE_N]
    hubs.sort(key=lambda n: len(n.provenances), reverse=True)
    hub_samples = [_sample_node(n) for n in hubs[:TOP_HUB_N]]
    if hubs:
        results.append(_check_result(
            "hub_spot_check", "warn",
            count=len(hubs),
            message=f"{len(hubs)} hub con ≥ {HIGH_PROVENANCE_N} provenances — spot-check consigliato.",
            samples=hub_samples,
        ))
    else:
        results.append(_check_result(
            "hub_spot_check", "pass",
            message="Nessun hub oltre soglia provenance.",
        ))

    # merged non trivial
    mn = metrics["merged_nodes_non_trivial"]
    merge_samples = [
        _sample_node(n) for n in graph.nodes
        if n.merge_method not in ("none", "exact")
    ][:MAX_SAMPLES]
    if mn:
        results.append(_check_result(
            "merged_nodes_audit", "info",
            count=mn,
            message=f"{mn} nodi con merge_method non banale.",
            samples=merge_samples,
        ))
    else:
        results.append(_check_result(
            "merged_nodes_audit", "pass",
            message="Solo merge exact o nodi singoli.",
        ))

    # echoes
    echoes = metrics["narrative_arcs"]["ECHOES"]
    info_below = CHECK_CATALOG["low_echoes_count"]["thresholds"]["info_below"]
    results.append(_check_result(
        "low_echoes_count", "info",
        count=echoes, target=f"< {info_below} atteso",
        message=f"{echoes} ECHOES — normale pre-enrich; cross-chunk in stadio 5.",
    ))

    # enrich proposals
    ep = metrics["enrich_proposals_pending"]
    ep_samples = []
    if resolver_log:
        for prop in resolver_log.get("stage6_proposals", [])[:MAX_SAMPLES]:
            ep_samples.append({
                "kind": prop.get("kind"),
                "source_id": prop["source_id"],
                "target_id": prop["target_id"],
                "note": prop.get("note"),
            })
    results.append(_check_result(
        "enrich_proposals_pending", "info",
        count=ep,
        message=f"{ep} proposte EMBODIES in attesa di stadio 5 enrich.",
        samples=ep_samples,
    ))

    # human validated
    hv = metrics["human_validated"]
    pct_hv = round(100 * hv["nodes_fully_validated"] / hv["nodes_total"], 2) if hv["nodes_total"] else 0
    results.append(_check_result(
        "human_validation_coverage", "info",
        value=pct_hv,
        message=f"{hv['nodes_fully_validated']}/{hv['nodes_total']} nodi fully human_validated (atteso 0% ora).",
    ))

    return results


def build_review_queue(graph: ResolvedGraph, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Solo item che meritano review umana — non tutti i warn aggregati."""
    items: list[dict[str, Any]] = []

    for n in graph.nodes:
        if n.review_needed:
            items.append({
                "priority": 1,
                "kind": "node",
                "entity_id": n.id,
                "entity_type": n.type.value,
                "reason": n.review_reason,
                "chunk_ids": _node_chunk_ids(n),
                "sample": _sample_node(n),
            })

    for e in graph.edges:
        if e.review_needed:
            items.append({
                "priority": 1,
                "kind": "edge",
                "entity_id": f"{e.source_id}|{e.type.value}|{e.target_id}",
                "entity_type": e.type.value,
                "reason": e.review_reason,
                "chunk_ids": sorted({p.chunk_id for p in e.provenances}),
            })

    hub_check = next((c for c in checks if c["check_id"] == "hub_spot_check"), None)
    if hub_check and hub_check["samples"]:
        for i, s in enumerate(hub_check["samples"]):
            items.append({
                "priority": 2,
                "kind": "node",
                "entity_id": s["id"],
                "entity_type": s["type"],
                "reason": "hub_spot_check",
                "chunk_ids": s.get("chunk_ids", []),
                "sample": s,
            })

    items.sort(key=lambda x: (x["priority"], x["entity_id"]))
    return {"total": len(items), "items": items}


def compute_verdict(checks: list[dict[str, Any]]) -> dict[str, Any]:
    fails = [c for c in checks if c["status"] == "fail" and c["blocks_stage"]]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        verdict: Verdict = "fail"
        headline = "Stadio 4 NON convalidato — correggere prima di enrich."
    elif warns:
        verdict = "pass_with_warnings"
        headline = "Stadio 4 convalidato con avvisi — review consigliata, si può procedere."
    else:
        verdict = "pass"
        headline = "Stadio 4 convalidato — pronto per stadio 5 enrich."

    return {
        "verdict": verdict,
        "headline": headline,
        "fail_count": len(fails),
        "warn_count": len(warns),
        "blocking_checks": [c["check_id"] for c in fails],
        "warning_checks": [c["check_id"] for c in warns],
    }


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
    cards = [
        ("Nodi", m["nodes_total"]),
        ("Archi", m["edges_total"]),
        ("Event", m["events"]["total"]),
        ("Event → Era", f"{m['events']['pct_with_during_era']}%"),
        ("CAUSED/FOLLOWS", m["narrative_arcs"]["caused_follows_ratio"]),
        ("ECHOES", m["narrative_arcs"]["ECHOES"]),
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
                if "name" in s:
                    lines.append(f"<li><code>{html.escape(s['id'])}</code> — {html.escape(s.get('name',''))}</li>")
                elif "source_id" in s:
                    lines.append(
                        f"<li><code>{html.escape(s['source_id'])}</code> → "
                        f"<code>{html.escape(s['target_id'])}</code></li>"
                    )
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
  <title>Health checkup — Stadio 4 Resolve</title>
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
  <h1>Health checkup — Stadio 4 Resolve</h1>
  <p class="meta">
    stage_4-5_health_checkup v{STAGE_VERSION} ·
    {html.escape(health_log.get('timestamp', ''))} ·
    input: <code>{html.escape(health_log.get('input_graph', ''))}</code>
  </p>

  {_verdict_banner(verdict)}

  <h2>Metriche rapide</h2>
  <div class="cards">{cards_html}</div>

  <h2>Guida ai controlli</h2>
  <div class="guide">
    <p>Questo checkpoint convalida che la <strong>deduplica</strong> sia riuscita.
       I warn su Era/ECHOES sono spesso legati all'<strong>estrazione</strong> (stadio 3)
       o all'<strong>enrich</strong> (stadio 5), non al merge.</p>
    <ul>{guide_items}</ul>
    <p class="muted">Dettaglio completo nel docstring di <code>src/stage_4-5_health_checkup.py</code>
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
  <p class="muted">Solo hub e review_needed — non tutti i warn aggregati.
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


def run(
    input_path: Path = INPUT_GRAPH,
    log_path: Path = INPUT_LOG,
    out_dir: Path = OUT_DIR,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    graph = _load_graph(input_path)
    resolver_log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else None

    metrics = compute_metrics(graph, resolver_log)
    # Rimuovi liste interne dai metrics pubblici
    public_metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}

    checks = run_checks(graph, metrics, resolver_log)
    verdict = compute_verdict(checks)
    review_queue = build_review_queue(graph, checks)

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(p)

    health_log = {
        "stage": "stage_4-5_health_checkup",
        "stage_version": STAGE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_graph": _rel(input_path),
        "input_resolver_log": _rel(log_path) if log_path.is_file() else None,
        "source_run": graph.source_run,
        "source_schema_version": graph.source_schema_version,
        "source_prompt_version": graph.source_prompt_version,
        "dedup_schema_version": graph.dedup_schema_version,
        "parameters": {
            "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
            "top_hub_n": TOP_HUB_N,
            "high_provenance_n": HIGH_PROVENANCE_N,
        },
        "verdict": verdict,
        "summary": {
            "nodes": public_metrics["nodes_total"],
            "edges": public_metrics["edges_total"],
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
    run()
