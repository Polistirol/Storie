# src/stage_5-5_transforms.py
"""
Stadio 5-5 — Archi TRANSFORMS_INTO (Phase -> Phase).

Ultimo arricchimento dello stadio 5. Cattura le Phase che EVOLVONO l'una
nell'altra (cambiamento qualitativo/interiore), non la semplice successione
cronologica (che è già implicita nell'aggancio OCCURS_IN alla stessa Era).

Scope (vedi ADR-023 / discussione 5-5):
- Person -> Person: FUORI SCOPE. Dopo la deduplica il soggetto è un solo nodo
  (`adriano`); non esistono due versioni-nodo della stessa persona fra cui porre
  un TRANSFORMS_INTO. Lato strutturalmente vuoto.
- Era -> Era: già generate deterministicamente nello stadio 3.5.
- Phase -> Phase: QUESTO modulo.

Candidati (deterministico)
--------------------------
- Raggruppa le Phase per Era (arco OCCURS_IN Phase -> Era).
- Dentro ogni Era, ordina le Phase per ORDINALE-CHUNK MEDIO (la media degli
  indici di chunk delle loro provenienze: proxy temporale robusto, il libro è
  grosso modo cronologico).
- Candidati = coppie di Phase CONSECUTIVE nell'ordine così ottenuto, dentro la
  stessa Era. Niente coppie fra ere diverse (il salto fra ere è la catena
  Era->Era del 3.5) né fra Phase non confinanti.

Giudizio (Qwen, LM Studio)
--------------------------
Per ogni coppia consecutiva (prima -> dopo): la prima fase si TRASFORMA/evolve
nella seconda, o è semplice successione? -> {reasoning, transforms, confidence}.
Bias verso "no": TRANSFORMS_INTO è un'evoluzione qualitativa (una fase che
diventa un'altra), non due fasi che si limitano a susseguirsi nel tempo.

Posa archi
----------
transforms=true e confidence >= --min-confidence (default 0.6) -> arco
TRANSFORMS_INTO Phase(prima) -> Phase(dopo). Dedup, validazione ResolvedGraph.

Input  (default: data/stage_5/4_echoes/enriched_graph.json — output 5-4)
Output (default: data/stage_5/5_transforms/)
- enriched_graph.json   (grafo + nuovi TRANSFORMS_INTO)
- transforms_map.json    (ordine per Era, coppie, verdetti, decisioni)
- transforms_cache.json  (verdetti grezzi Qwen)

File unico: --dry-run mostra solo l'ordinamento e le coppie (niente Qwen).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from src.schema import EdgeType, NodeType, Provenance, SCHEMA_VERSION
from src.deduplication_schema import (
    DEDUP_SCHEMA_VERSION,
    ResolvedEdge,
    ResolvedGraph,
    ResolvedNode,
    align_edge_endpoint_types,
)

PROMPT_VERSION = "0.1.0"
STAGE_VERSION = "0.1.0"
ENRICH_MODEL = "stage_5-5_transforms"
DEFAULT_URL = "http://localhost:1234/v1"
DEFAULT_MIN_CONFIDENCE = 0.6

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "4_echoes" / "enriched_graph.json"
DEFAULT_OUT_DIR = _PROJECT_ROOT / "data" / "stage_5" / "5_transforms"


SYSTEM_PROMPT = """\
Sei un analista biografico per un knowledge graph sulle "Memorie di Adriano" di \
Yourcenar. Ricevi DUE fasi di vita (Phase) CONSECUTIVE nella stessa epoca, \
ciascuna con id, nome e descrizione, nell'ordine temporale PRIMA -> DOPO. \
Stabilisci se la prima si TRASFORMA nella seconda.

COS'È UNA TRASFORMAZIONE. La prima fase si trasforma nella seconda quando la \
seconda è un'EVOLUZIONE qualitativa della prima: la stessa linea di vita che \
cambia di natura, matura, si rovescia o si approfondisce (es. l'apprendistato \
militare che diventa comando autonomo; l'entusiasmo del primo regno che si fa \
disillusione; la malattia che progredisce verso la fase terminale). C'è un filo \
di continuità che porta dall'una all'altra e un cambiamento di stato lungo quel \
filo.

NON è una trasformazione:
- due fasi che si limitano a SUCCEDERSI nel tempo senza che una nasca dall'altra \
(la successione cronologica è già registrata altrove: NON serve un arco per \
dire "B viene dopo A");
- due fasi parallele o indipendenti che capitano vicine;
- due fasi che condividono solo il tema o l'epoca.

BIAS: nel dubbio "transforms": false. L'arco va posato solo quando c'è una vera \
metamorfosi della stessa linea di vita, non per ogni coppia consecutiva.

Restituisci: "reasoning" (1-2 frasi: qual è la continuità e il cambiamento di \
stato, o perché è semplice successione), "transforms" (true/false), \
"confidence" (0.0-1.0)."""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "phase_transform",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "transforms": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["reasoning", "transforms", "confidence"],
            "additionalProperties": False,
        },
    },
}


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class TransformDecision(BaseModel):
    era: str
    phase_before: str
    phase_after: str
    name_before: str
    name_after: str
    transforms: Optional[bool] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    action: str = "judged"      # judged | materialized | skipped_low_conf | skipped_no_transform | error
    from_cache: bool = False


class EraOrder(BaseModel):
    era: str
    phases: list[str] = Field(default_factory=list)   # ordinate per chunk medio


class TransformsMap(BaseModel):
    source_graph: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    prompt_version: str = PROMPT_VERSION
    model: str
    min_confidence: float
    n_phases: int
    n_eras: int
    n_pairs: int
    n_materialized: int
    era_orders: list[EraOrder] = Field(default_factory=list)
    decisions: list[TransformDecision] = Field(default_factory=list)


TransformDecision.model_rebuild()
EraOrder.model_rebuild()
TransformsMap.model_rebuild()


# -----------------------------------------------------------------------------
# Phase + ordinamento per chunk medio
# -----------------------------------------------------------------------------

_CHUNK_RE = re.compile(r"(\d+)")


def _chunk_ordinal(chunk_id: Optional[str]) -> Optional[int]:
    if not chunk_id:
        return None
    m = _CHUNK_RE.search(chunk_id)
    return int(m.group(1)) if m else None


def _mean_ordinal(node: ResolvedNode) -> float:
    ords = [o for o in (_chunk_ordinal(p.chunk_id) for p in node.provenances) if o is not None]
    return sum(ords) / len(ords) if ords else float("inf")


def build_era_groups(graph: ResolvedGraph) -> tuple[dict[str, list[ResolvedNode]], dict[str, ResolvedNode]]:
    """Raggruppa le Phase per Era (via OCCURS_IN), ordinate per chunk medio."""
    phases = {n.id: n for n in graph.nodes if n.type == NodeType.PHASE}
    # OCCURS_IN: Phase -> Era
    era_of: dict[str, str] = {}
    for e in graph.edges:
        if e.type == EdgeType.OCCURS_IN and e.source_id in phases:
            era_of[e.source_id] = e.target_id
    groups: dict[str, list[ResolvedNode]] = defaultdict(list)
    for pid, node in phases.items():
        era = era_of.get(pid)
        if era is None:
            continue  # Phase senza Era: niente coppie (no asse temporale)
        groups[era].append(node)
    for era in groups:
        groups[era].sort(key=lambda n: (_mean_ordinal(n), n.id))
    return groups, phases


def consecutive_pairs(groups: dict[str, list[ResolvedNode]]) -> list[tuple[str, ResolvedNode, ResolvedNode]]:
    pairs = []
    for era, nodes in groups.items():
        for a, b in zip(nodes, nodes[1:]):
            pairs.append((era, a, b))
    return pairs


# -----------------------------------------------------------------------------
# LLM
# -----------------------------------------------------------------------------

def _make_client(url: str):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("openai non installato.") from exc
    return OpenAI(base_url=url, api_key="lm-studio")


def _resolve_model(client, requested, url):
    if requested:
        return requested
    try:
        return client.models.list().data[0].id
    except Exception as exc:
        raise SystemExit(f"LM Studio non raggiungibile su {url}: {exc}") from exc


def _extract_json(content: str) -> dict:
    s = (content or "").strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def _build_user(a: ResolvedNode, b: ResolvedNode) -> str:
    return (
        "Fase PRIMA:\n"
        f"  id: {a.id}\n  nome: {a.name}\n"
        f"  descrizione: {a.description or '(nessuna descrizione)'}\n\n"
        "Fase DOPO:\n"
        f"  id: {b.id}\n  nome: {b.name}\n"
        f"  descrizione: {b.description or '(nessuna descrizione)'}\n\n"
        "La fase PRIMA si trasforma nella fase DOPO, o è semplice successione? "
        "Rispondi solo con l'oggetto JSON. /no_think"
    )


def _cache_key(a_id: str, b_id: str, model_id: str) -> str:
    raw = a_id + "->" + b_id + "||" + model_id + "||" + PROMPT_VERSION
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(graph_path: Path, out_dir: Path, model: Optional[str], url: str,
        min_confidence: float, temperature: float, seed: int,
        use_cache: bool, refresh: bool, dry_run: bool) -> Optional[TransformsMap]:
    with graph_path.open("r", encoding="utf-8") as f:
        graph = ResolvedGraph(**json.load(f))

    groups, phases = build_era_groups(graph)
    pairs = consecutive_pairs(groups)
    n_phases = len(phases)
    print(f"Phase: {n_phases}   Era con Phase: {len(groups)}   coppie consecutive: {len(pairs)}")

    era_orders = [EraOrder(era=era, phases=[n.id for n in nodes]) for era, nodes in groups.items()]

    if dry_run:
        print("\n--- DRY-RUN ordinamento + coppie (nessuna chiamata Qwen) ---")
        for eo in era_orders:
            print(f"  Era {eo.era}: {len(eo.phases)} phase")
            for pid in eo.phases:
                print(f"     {pid}  (chunk medio {_mean_ordinal(phases[pid]):.0f})")
        print(f"\n  -> {len(pairs)} coppie consecutive da giudicare")
        return None

    client = _make_client(url)
    model_id = _resolve_model(client, model, url)
    print(f"modello: {model_id}   prompt_version: {PROMPT_VERSION}")

    cache_path = out_dir / "transforms_cache.json"
    cache: dict = {}
    if use_cache and not refresh and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc)
    decisions: list[TransformDecision] = []
    new_edges: list[ResolvedEdge] = []
    existing_keys = {(e.source_id, e.type, e.target_id, e.role) for e in graph.edges}
    n_hits = 0

    for idx, (era, a, b) in enumerate(pairs, 1):
        key = _cache_key(a.id, b.id, model_id)
        raw, from_cache = None, False
        if use_cache and not refresh and key in cache:
            raw, from_cache = cache[key]["result"], True
            n_hits += 1
        if raw is None:
            try:
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT},
                              {"role": "user", "content": _build_user(a, b)}],
                    temperature=temperature, seed=seed, max_tokens=400,
                    response_format=RESPONSE_FORMAT)
                raw = _extract_json(resp.choices[0].message.content)
            except Exception as exc:
                decisions.append(TransformDecision(era=era, phase_before=a.id, phase_after=b.id,
                                                   name_before=a.name, name_after=b.name,
                                                   action="error", reasoning=str(exc)[:160]))
                continue
            if use_cache:
                cache[key] = {"phase_before": a.id, "phase_after": b.id, "result": raw,
                              "timestamp": now.isoformat()}
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

        transforms = bool(raw.get("transforms"))
        conf = raw.get("confidence")
        dec = TransformDecision(era=era, phase_before=a.id, phase_after=b.id,
                                name_before=a.name, name_after=b.name,
                                transforms=transforms, confidence=conf,
                                reasoning=raw.get("reasoning"), from_cache=from_cache)

        if not transforms:
            dec.action = "skipped_no_transform"
        elif conf is None or conf < min_confidence:
            dec.action = "skipped_low_conf"
        else:
            key_e = (a.id, EdgeType.TRANSFORMS_INTO, b.id, None)
            if key_e in existing_keys:
                dec.action = "skipped_no_transform"
                dec.reasoning = (dec.reasoning or "") + " [arco già presente]"
            else:
                ground = sorted({p.chunk_id for p in a.provenances})
                prov = Provenance(
                    chunk_id=ground[0] if ground else "UNKNOWN", model=ENRICH_MODEL,
                    timestamp=now, schema_version=SCHEMA_VERSION, confidence=conf,
                    evidence_span=(dec.reasoning or "")[:200] or None, human_validated=False)
                new_edges.append(ResolvedEdge(
                    source_id=a.id, target_id=b.id, type=EdgeType.TRANSFORMS_INTO,
                    source_type=NodeType.PHASE, target_type=NodeType.PHASE,
                    description=dec.reasoning, role=None, provenances=[prov],
                    merged_from=[f"stage5_transforms:{a.id}|TRANSFORMS_INTO|{b.id}"],
                    merge_confidence=conf, review_needed=True,
                    review_reason=f"TRANSFORMS_INTO inferito in 5-5 (Era {era}, conf {conf})"))
                existing_keys.add(key_e)
                dec.action = "materialized"
        decisions.append(dec)

    enriched = ResolvedGraph(
        nodes=graph.nodes, edges=graph.edges + new_edges,
        source_run=graph.source_run, source_schema_version=graph.source_schema_version,
        source_prompt_version=graph.source_prompt_version,
        dedup_schema_version=DEDUP_SCHEMA_VERSION, stage_version=graph.stage_version,
        timestamp=now)
    enriched = align_edge_endpoint_types(enriched)

    tmap = TransformsMap(
        source_graph=str(graph_path), timestamp=now, model=model_id,
        min_confidence=min_confidence, n_phases=n_phases, n_eras=len(groups),
        n_pairs=len(pairs), n_materialized=len(new_edges),
        era_orders=era_orders, decisions=decisions)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "enriched_graph.json").write_text(enriched.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "transforms_map.json").write_text(tmap.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(tmap, n_hits)
    return tmap


def _print_summary(t: TransformsMap, n_hits: int) -> None:
    yes = sum(1 for d in t.decisions if d.transforms)
    low = sum(1 for d in t.decisions if d.action == "skipped_low_conf")
    err = sum(1 for d in t.decisions if d.action == "error")
    print("\n--- riepilogo 5-5 (TRANSFORMS_INTO Phase->Phase) ---")
    print(f"  Phase              : {t.n_phases}   Era: {t.n_eras}")
    print(f"  coppie consecutive : {t.n_pairs}   (cache hit: {n_hits})")
    print(f"  giudicate 'transforms' : {yes}")
    print(f"  materializzate     : {t.n_materialized}")
    print(f"  scartate (conf<{t.min_confidence}) : {low}")
    if err:
        print(f"  errori             : {err}")
    mats = [d for d in t.decisions if d.action == "materialized"]
    if mats:
        print("  --- archi posati ---")
        for d in sorted(mats, key=lambda d: -(d.confidence or 0)):
            print(f"   conf={d.confidence:.2f}  [{d.era}]  {d.phase_before}  =>  {d.phase_after}")
            if d.reasoning:
                print(f"        {d.reasoning[:140]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-5: archi TRANSFORMS_INTO Phase->Phase.")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="enriched_graph.json del 5-4")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--model", default=None, help="modello Qwen (default: quello su LM Studio)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="solo ordinamento+coppie, niente Qwen")
    args = ap.parse_args()

    run(args.graph, args.out, model=args.model, url=args.url,
        min_confidence=args.min_confidence, temperature=args.temperature, seed=args.seed,
        use_cache=not args.no_cache, refresh=args.refresh, dry_run=args.dry_run)


if __name__ == "__main__":
    main()