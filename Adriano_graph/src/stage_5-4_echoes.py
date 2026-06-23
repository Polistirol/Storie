# src/stage_5-4_echoes.py
"""
Stadio 5-4 — Archi ECHOES (eco narrativo Event -> Event).

A differenza del 5-3 (che costruiva una STRUTTURA globale: gerarchia, cappelli,
cicli), ECHOES sono archi SCIOLTI: "questa scena richiama quest'altra". Nessun
nodo nuovo, nessun cappello, nessuna gerarchia, nessun ciclo da rompere. È lo
stesso pattern del 5-2 (candidati -> giudizio), quindi sta in UN solo modulo con
due modalità:

  --dry-run : solo i candidati (BGE-M3, niente Qwen). Per tarare soglia e k.
  run piena : giudica le coppie con Qwen e posa gli archi ECHOES nel grafo.

Candidati (deterministico, embedder)
------------------------------------
- Embedde la `description` di ogni Event (BGE-M3); per ciascuno i top-k vicini
  per coseno. Coppia non orientata, dedup.
- ESCLUDE le coppie adiacenti: stesso chunk o chunk consecutivi (|Δ| <
  `--min-chunk-gap`, default 3). ECHOES collega scene LONTANE, non la scena
  accanto (quella è già FOLLOWS/CAUSED dell'estrazione).
- Soglia di coseno `--min-cosine` (default 0.55) + cap `--max-per-event`
  (default 5) per non far esplodere il numero di coppie.

Giudizio (Qwen, LM Studio)
--------------------------
Per ogni coppia: è un eco narrativo? in che direzione? -> {reasoning, echoes,
direction, confidence}. Bias verso "no": ECHOES deve essere una ripresa/risonanza
voluta, non semplice somiglianza tematica (quella è già catturata dai Theme).
Cache idempotente per (event_a, event_b, model, prompt_version).

Posa archi
----------
Solo le coppie echoes=true con confidence >= `--min-confidence` (default 0.6)
diventano ECHOES. Direzione: l'evento richiamato (più antico / la "fonte") è il
target; chi richiama è il source. Dedup contro archi esistenti per
(source, type, target, role). Validazione finale via ResolvedGraph.

Input  (default: data/stage_5/3_hierarchy/enriched_graph.json — output 5-3d)
Output (default: data/stage_5/4_echoes/)
- enriched_graph.json   (grafo + nuovi ECHOES)
- echoes_map.json        (candidati + verdetti + decisioni, ispezionabile)
- echoes_cache.json      (verdetti grezzi Qwen)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from pydantic import BaseModel, Field

from src.schema import EdgeType, NodeType, Provenance, SCHEMA_VERSION
from src.deduplication_schema import (
    DEDUP_SCHEMA_VERSION,
    ResolvedEdge,
    ResolvedGraph,
    ResolvedNode,
    align_edge_endpoint_types,
)

PROMPT_VERSION = "0.2.0"
STAGE_VERSION = "0.1.0"
ENRICH_MODEL = "stage_5-4_echoes"

DEFAULT_URL = "http://localhost:1234/v1"
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_MIN_COSINE = 0.55
DEFAULT_MAX_PER_EVENT = 5
DEFAULT_MIN_CHUNK_GAP = 3
DEFAULT_MIN_CONFIDENCE = 0.6

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "3_hierarchy" / "enriched_graph.json"
DEFAULT_OUT_DIR = _PROJECT_ROOT / "data" / "stage_5" / "4_echoes"


SYSTEM_PROMPT = """\
Sei un analista narrativo per un knowledge graph biografico sulle "Memorie di \
Adriano" di Yourcenar. Ricevi DUE Event (due scene/episodi della vita del \
narratore), ciascuno con id, nome e descrizione. Devi stabilire se tra i due \
esiste un ECO NARRATIVO.

COS'È UN ECO NARRATIVO. Una scena fa eco a un'altra quando la riprende o la \
rispecchia. L'eco può essere di due tipi, ENTRAMBI validi:
- ESPLICITO: il narratore richiama una scena passata mentre ne vive un'altra \
(un gesto che ripete un gesto precedente, un ricordo che riaffiora).
- STRUTTURALE: due episodi dello STESSO TIPO che si ripetono a distanza nel \
tempo e si fanno specchio, ANCHE se il testo non rimanda esplicitamente dall'uno \
all'altro. Esempi: due nomine/investiture della stessa persona a cariche \
diverse; due addii; due morti che si rispondono; due viaggi paralleli; due \
prove di potere in momenti diversi della vita. La ripetizione di un atto a \
distanza È un eco strutturale: NON serve che il narratore lo dichiari.

Non ti serve trovare un richiamo "deliberato" o esplicito: se i due Event sono \
varianti riconoscibili dello stesso schema narrativo accadute in momenti \
diversi, è un eco.

NON è un eco:
- LO STESSO IDENTICO FATTO descritto due volte (stesso soggetto, stesso \
episodio storico: es. "la campagna partica di Traiano" e "la spedizione di \
Traiano contro i Parti"). Quelli non sono due scene che si fanno eco: sono lo \
stesso evento duplicato. In questo caso "echoes": false.
- due scene che condividono solo il tema generale (la morte, il potere) senza \
ripetere uno schema o un atto: quella connessione è già nei Theme;
- due scene vicine nel tempo legate da semplice conseguenza: quella è \
FOLLOWS/CAUSED, non ECHOES;
- somiglianza solo di parole o di entità citate.

DIREZIONE. Se c'è eco, indica quale evento è la FONTE (quello richiamato, di \
norma il più antico o originario) e quale lo RICHIAMA. "direction" = \
"a_echoes_b" se A richiama B (B è la fonte), "b_echoes_a" se B richiama A.

BIAS: distingui con cura l'eco vero (due scene diverse che si rispecchiano) dal \
duplicato (la stessa scena due volte) e dalla semplice affinità tematica. Nel \
dubbio fra eco strutturale e affinità vaga, chiediti: è lo stesso TIPO di atto \
o di situazione, ripetuto in un altro momento della vita? Se sì, è un eco.

Restituisci: "reasoning" (1-2 frasi: qual è la ripresa o lo schema ripetuto, o \
perché non c'è / è un duplicato), "echoes" (true/false), "direction" \
("a_echoes_b" | "b_echoes_a" | null se echoes=false), "confidence" (0.0-1.0)."""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "narrative_echo",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "echoes": {"type": "boolean"},
                "direction": {"type": ["string", "null"],
                              "enum": ["a_echoes_b", "b_echoes_a", None]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["reasoning", "echoes", "direction", "confidence"],
            "additionalProperties": False,
        },
    },
}


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class EchoDecision(BaseModel):
    event_a: str
    event_b: str
    name_a: str
    name_b: str
    cosine: float
    echoes: Optional[bool] = None
    direction: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    action: str = "judged"          # judged | materialized | skipped_low_conf | skipped_no_echo | error
    from_cache: bool = False


class EchoesMap(BaseModel):
    source_graph: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    prompt_version: str = PROMPT_VERSION
    model: str
    embed_model: str
    min_cosine: float
    max_per_event: int
    min_chunk_gap: int
    min_confidence: float
    n_events: int
    n_candidates: int
    n_materialized: int
    decisions: list[EchoDecision] = Field(default_factory=list)


EchoDecision.model_rebuild()
EchoesMap.model_rebuild()


# -----------------------------------------------------------------------------
# Event + chunk index
# -----------------------------------------------------------------------------

_CHUNK_RE = re.compile(r"(\d+)")


def _chunk_ordinal(chunk_id: Optional[str]) -> Optional[int]:
    """ch_0166 -> 166. Per misurare la distanza fra Event nel testo."""
    if not chunk_id:
        return None
    m = _CHUNK_RE.search(chunk_id)
    return int(m.group(1)) if m else None


def load_events(graph: ResolvedGraph) -> list[dict]:
    out = []
    for n in graph.nodes:
        if n.type == NodeType.EVENT:
            ords = sorted({o for o in (_chunk_ordinal(p.chunk_id) for p in n.provenances) if o is not None})
            out.append({
                "id": n.id,
                "name": n.name,
                "description": n.description or "",
                "chunk_ords": ords,
                "first_chunk": sorted({p.chunk_id for p in n.provenances})[0] if n.provenances else None,
            })
    return out


def _min_gap(a: dict, b: dict) -> Optional[int]:
    """Minima distanza (in indici di chunk) fra due Event. None se ignota."""
    if not a["chunk_ords"] or not b["chunk_ords"]:
        return None
    return min(abs(x - y) for x in a["chunk_ords"] for y in b["chunk_ords"])


# -----------------------------------------------------------------------------
# Candidati (embedder)
# -----------------------------------------------------------------------------

def embed(texts: list[str], model_name: str, device: Optional[str]) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("sentence-transformers non installato.") from exc
    model = SentenceTransformer(model_name, device=device)
    return model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                        show_progress_bar=True, batch_size=32).astype(np.float32)


def build_candidates(events: list[dict], emb: np.ndarray, min_cosine: float,
                     max_per_event: int, min_chunk_gap: int) -> list[dict]:
    """Coppie non orientate (i<j) per top-k vicini, escluse le adiacenti."""
    sims = emb @ emb.T
    n = len(events)
    seen: set[tuple[int, int]] = set()
    cands: list[dict] = []
    for i in range(n):
        order = np.argsort(-sims[i])
        taken = 0
        for j in order:
            j = int(j)
            if j == i:
                continue
            c = float(sims[i, j])
            if c < min_cosine:
                break  # ordinati decrescenti: sotto soglia, stop
            lo, hi = (i, j) if i < j else (j, i)
            if (lo, hi) in seen:
                continue
            gap = _min_gap(events[i], events[j])
            if gap is not None and gap < min_chunk_gap:
                continue  # adiacenti: non è ECHOES
            seen.add((lo, hi))
            cands.append({"i": lo, "j": hi, "cosine": float(sims[lo, hi])})
            taken += 1
            if taken >= max_per_event:
                break
    cands.sort(key=lambda d: -d["cosine"])
    return cands


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


def _build_user(a: dict, b: dict) -> str:
    return (
        "Event A:\n"
        f"  id: {a['id']}\n  nome: {a['name']}\n"
        f"  descrizione: {a['description'] or '(nessuna descrizione)'}\n\n"
        "Event B:\n"
        f"  id: {b['id']}\n  nome: {b['name']}\n"
        f"  descrizione: {b['description'] or '(nessuna descrizione)'}\n\n"
        "C'è un eco narrativo fra A e B? Rispondi solo con l'oggetto JSON. /no_think"
    )


def _cache_key(a_id: str, b_id: str, model_id: str) -> str:
    raw = "|".join(sorted((a_id, b_id))) + "||" + model_id + "||" + PROMPT_VERSION
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(graph_path: Path, out_dir: Path, embed_model: str, model: Optional[str],
        url: str, min_cosine: float, max_per_event: int, min_chunk_gap: int,
        min_confidence: float, temperature: float, seed: int, device: Optional[str],
        use_cache: bool, refresh: bool, dry_run: bool) -> Optional[EchoesMap]:
    with graph_path.open("r", encoding="utf-8") as f:
        graph = ResolvedGraph(**json.load(f))
    events = load_events(graph)
    if len(events) < 2:
        raise SystemExit("meno di 2 Event nel grafo")
    print(f"Event: {len(events)}   embedding con {embed_model} ...")

    emb = embed([e["description"] or e["name"] for e in events], embed_model, device)
    cands = build_candidates(events, emb, min_cosine, max_per_event, min_chunk_gap)
    print(f"coppie candidate (cos>={min_cosine}, gap>={min_chunk_gap}, max {max_per_event}/event): {len(cands)}")

    if dry_run:
        print("\n--- DRY-RUN candidati (nessuna chiamata Qwen) ---")
        for c in cands[:80]:
            a, b = events[c["i"]], events[c["j"]]
            print(f"  cos={c['cosine']:.3f}  {a['id']}  ~  {b['id']}")
        if len(cands) > 80:
            print(f"  ... e altre {len(cands) - 80}")
        return None

    client = _make_client(url)
    model_id = _resolve_model(client, model, url)
    print(f"modello: {model_id}   prompt_version: {PROMPT_VERSION}")

    cache_path = out_dir / "echoes_cache.json"
    cache: dict = {}
    if use_cache and not refresh and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc)
    decisions: list[EchoDecision] = []
    new_edges: list[ResolvedEdge] = []
    existing_keys = {(e.source_id, e.type, e.target_id, e.role) for e in graph.edges}
    n_hits = 0

    for idx, c in enumerate(cands, 1):
        a, b = events[c["i"]], events[c["j"]]
        key = _cache_key(a["id"], b["id"], model_id)
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
                decisions.append(EchoDecision(event_a=a["id"], event_b=b["id"],
                                              name_a=a["name"], name_b=b["name"],
                                              cosine=c["cosine"], action="error",
                                              reasoning=str(exc)[:160]))
                continue
            if use_cache:
                cache[key] = {"event_a": a["id"], "event_b": b["id"], "result": raw,
                              "timestamp": now.isoformat()}
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

        echoes = bool(raw.get("echoes"))
        direction = raw.get("direction")
        conf = raw.get("confidence")
        dec = EchoDecision(event_a=a["id"], event_b=b["id"], name_a=a["name"], name_b=b["name"],
                           cosine=c["cosine"], echoes=echoes, direction=direction,
                           confidence=conf, reasoning=raw.get("reasoning"), from_cache=from_cache)

        if not echoes:
            dec.action = "skipped_no_echo"
        elif conf is None or conf < min_confidence:
            dec.action = "skipped_low_conf"
        else:
            # direzione: source richiama target (target = fonte/più antico)
            if direction == "b_echoes_a":
                src_id, tgt_id = b["id"], a["id"]
            else:  # a_echoes_b o None -> default A richiama B
                src_id, tgt_id = a["id"], b["id"]
            key_e = (src_id, EdgeType.ECHOES, tgt_id, None)
            if key_e in existing_keys:
                dec.action = "skipped_no_echo"
                dec.reasoning = (dec.reasoning or "") + " [arco già presente]"
            else:
                src_ev = events[c["i"]] if src_id == a["id"] else events[c["j"]]
                prov = Provenance(
                    chunk_id=src_ev["first_chunk"] or "UNKNOWN", model=ENRICH_MODEL,
                    timestamp=now, schema_version=SCHEMA_VERSION, confidence=conf,
                    evidence_span=(dec.reasoning or "")[:200] or None, human_validated=False)
                new_edges.append(ResolvedEdge(
                    source_id=src_id, target_id=tgt_id, type=EdgeType.ECHOES,
                    source_type=NodeType.EVENT, target_type=NodeType.EVENT,
                    description=dec.reasoning, role=None, provenances=[prov],
                    merged_from=[f"stage5_echoes:{src_id}|ECHOES|{tgt_id}"],
                    merge_confidence=conf, review_needed=True,
                    review_reason=f"ECHOES inferito in 5-4 (cos {c['cosine']:.2f}, conf {conf})"))
                existing_keys.add(key_e)
                dec.action = "materialized"
        decisions.append(dec)
        if idx % 50 == 0:
            print(f"  [{idx}/{len(cands)}] ...")

    enriched = ResolvedGraph(
        nodes=graph.nodes, edges=graph.edges + new_edges,
        source_run=graph.source_run, source_schema_version=graph.source_schema_version,
        source_prompt_version=graph.source_prompt_version,
        dedup_schema_version=DEDUP_SCHEMA_VERSION, stage_version=graph.stage_version,
        timestamp=now)
    enriched = align_edge_endpoint_types(enriched)

    emap = EchoesMap(
        source_graph=str(graph_path), timestamp=now, model=model_id, embed_model=embed_model,
        min_cosine=min_cosine, max_per_event=max_per_event, min_chunk_gap=min_chunk_gap,
        min_confidence=min_confidence, n_events=len(events), n_candidates=len(cands),
        n_materialized=len(new_edges), decisions=decisions)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "enriched_graph.json").write_text(enriched.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "echoes_map.json").write_text(emap.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(emap, n_hits)
    return emap


def _print_summary(e: EchoesMap, n_hits: int) -> None:
    yes = sum(1 for d in e.decisions if d.echoes)
    low = sum(1 for d in e.decisions if d.action == "skipped_low_conf")
    err = sum(1 for d in e.decisions if d.action == "error")
    print("\n--- riepilogo 5-4 (ECHOES) ---")
    print(f"  Event              : {e.n_events}")
    print(f"  coppie candidate   : {e.n_candidates}   (cache hit: {n_hits})")
    print(f"  giudicate 'echoes' : {yes}")
    print(f"  materializzate     : {e.n_materialized}")
    print(f"  scartate (conf<{e.min_confidence}) : {low}")
    if err:
        print(f"  errori             : {err}")
    mats = [d for d in e.decisions if d.action == "materialized"]
    if mats:
        print("  --- archi posati (prime 15) ---")
        for d in sorted(mats, key=lambda d: -(d.confidence or 0))[:15]:
            print(f"   conf={d.confidence:.2f}  {d.event_a}  ->ECHOES->  {d.event_b}")
            if d.reasoning:
                print(f"        {d.reasoning[:140]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-4: archi ECHOES Event->Event.")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH, help="enriched_graph.json del 5-3d")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--model-embed", default=DEFAULT_EMBED_MODEL, help="path locale bge-m3")
    ap.add_argument("--model", default=None, help="modello Qwen (default: quello su LM Studio)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--min-cosine", type=float, default=DEFAULT_MIN_COSINE)
    ap.add_argument("--max-per-event", type=int, default=DEFAULT_MAX_PER_EVENT)
    ap.add_argument("--min-chunk-gap", type=int, default=DEFAULT_MIN_CHUNK_GAP,
                    help="esclude coppie con chunk a distanza < gap (default 3)")
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="solo candidati, niente Qwen")
    args = ap.parse_args()

    run(args.graph, args.out, embed_model=args.model_embed, model=args.model, url=args.url,
        min_cosine=args.min_cosine, max_per_event=args.max_per_event,
        min_chunk_gap=args.min_chunk_gap, min_confidence=args.min_confidence,
        temperature=args.temperature, seed=args.seed, device=args.device,
        use_cache=not args.no_cache, refresh=args.refresh, dry_run=args.dry_run)


if __name__ == "__main__":
    main()