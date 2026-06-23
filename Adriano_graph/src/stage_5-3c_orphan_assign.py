# src/stage_5-3c_orphan_assign.py
"""
Stadio 5-3c — Aggancio degli orfani ai cappelli (FASE 2 del 5-3b/c/d).

Gli orfani sono i temi rimasti senza cappello dopo il 5-3b:
  - gli `unattached` di tutti i cluster giudicati;
  - i SINGOLETTI (cluster di dimensione 1 nel 5-3a), che il 5-3b ha saltato.
Per ciascuno si decide se SPECIALIZZA uno dei cappelli già stabiliti, oppure se
resta STANDALONE (foglia senza cappello). Gli orfani si agganciano SOLO ai
cappelli, mai fra loro (scelta conservativa: niente sotto-gerarchie inventate
fra singoletti).

Schema embedder+LLM, come nel resto dello stadio 5:
  - SHORTLIST (BGE-M3, recall): per ogni orfano, i top-K cappelli per coseno sul
    nome.
  - DECISIONE (Qwen, precisione): legge la DESCRIZIONE dell'orfano e i cappelli
    candidati (nome + membri d'esempio) e sceglie `specializes <cap_id>` oppure
    `standalone`. In dubbio -> standalone.

Universo dei cappelli = cappelli del 5-3b (esistenti promossi + nuovi
sintetizzati) UNITI ai cappelli del seed DAG del 5-3a, deduplicati per cap_id.

Input  (default: data/stage_5/3_hierarchy/ e 2_themes/)
  - hierarchy_judgments.json  (5-3b): cappelli + unattached
  - hierarchy_candidates.json (5-3a): cluster (per i singoletti) + seed_edges/caps
  - enriched_graph.json       (5-2c): name + description dei Theme
Output (default: data/stage_5/3_hierarchy/)
  - hierarchy_assignments.json (decisione per orfano, consumata dal 5-3d)
  - orphan_cache.json          (cache risposte grezze Qwen)

Uso:
  python -m src.stage_5-3c_orphan_assign --model-embed <bge-m3> --model qwen/qwen3-14b
  python -m src.stage_5-3c_orphan_assign --model-embed <bge-m3> --dry-run   # solo shortlist
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

PROMPT_VERSION = "0.1.0"
STAGE_VERSION = "0.1.0"
DEFAULT_URL = "http://localhost:1234/v1"
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_K = 5

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
DEFAULT_HIER_DIR = _PROJECT_ROOT / "data" / "stage_5" / "3_hierarchy"
DEFAULT_JUDGMENTS = DEFAULT_HIER_DIR / "hierarchy_judgments.json"
DEFAULT_CANDIDATES = DEFAULT_HIER_DIR / "hierarchy_candidates.json"
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "2_themes" / "enriched_graph.json"
DEFAULT_CACHE = DEFAULT_HIER_DIR / "orphan_cache.json"
DEFAULT_OUT = DEFAULT_HIER_DIR / "hierarchy_assignments.json"


SYSTEM_PROMPT = """\
Sei un organizzatore di temi per un knowledge graph biografico sulle "Memorie \
di Adriano" di Yourcenar. Ricevi UN tema "orfano" (nodo Theme con id, nome, \
descrizione) e una lista di CAPPELLI candidati (temi più generali, ciascuno con \
id, nome e alcuni temi-esempio che vi rientrano).

Compito: decidere se l'orfano è una DECLINAZIONE PIÙ SPECIFICA di uno dei \
cappelli candidati (quindi lo "specializza"), oppure se è un tema a sé che NON \
rientra sotto nessuno di essi (standalone).

Regole:
- Scegli "specializes" e indica il "cap_id" SOLO se l'orfano è chiaramente un \
caso particolare / una sfumatura di quel cappello. Il cap_id DEVE essere uno di \
quelli proposti.
- Scegli "standalone" (cap_id=null) se nessun cappello lo contiene davvero, o se \
l'orfano è altrettanto generale del cappello, o se il legame è solo vagamente \
associativo. NON forzare un aggancio.
- Decidi in base alla DESCRIZIONE, non solo al nome.
- In caso di dubbio: standalone.

Restituisci: "reasoning" (1-2 frasi), "decision" ("specializes"|"standalone"), \
"cap_id" (uno dei candidati, oppure null), "confidence" (0.0-1.0)."""


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class OrphanAssignment(BaseModel):
    orphan_id: str
    name: str
    source: str                       # "unattached" | "singleton"
    decision: str                     # "specializes" | "standalone"
    cap_id: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: str = ""
    shortlist: list[str] = Field(default_factory=list)
    top_cosine: Optional[float] = None
    from_cache: bool = False


class HierarchyAssignments(BaseModel):
    source_judgments: str
    source_candidates: str
    source_graph: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    prompt_version: str = PROMPT_VERSION
    model: str
    embed_model: str
    k: int
    n_orphans: int
    n_caps: int
    assignments: list[OrphanAssignment] = Field(default_factory=list)


OrphanAssignment.model_rebuild()
HierarchyAssignments.model_rebuild()


# -----------------------------------------------------------------------------
# Costruzione universo cappelli + orfani
# -----------------------------------------------------------------------------

def _theme_index(graph_path: Path) -> dict[str, dict]:
    g = json.loads(graph_path.read_text(encoding="utf-8"))
    return {n["id"]: {"name": n["name"], "description": n.get("description")}
            for n in g.get("nodes", []) if n.get("type") == "Theme"}


def build_caps(judgments_path: Path, candidates_path: Path,
               themes: dict[str, dict]) -> dict[str, dict]:
    """cap_id -> {cap_id, name, label, existing_id, is_new, examples:list[str names]}."""
    jud = json.loads(judgments_path.read_text(encoding="utf-8"))
    cand = json.loads(candidates_path.read_text(encoding="utf-8"))
    caps: dict[str, dict] = {}

    def ensure(cap_id, label, existing_id):
        if cap_id not in caps:
            name = themes.get(cap_id, {}).get("name") or label or cap_id
            caps[cap_id] = {"cap_id": cap_id, "name": name, "label": label,
                            "existing_id": existing_id, "is_new": existing_id is None,
                            "example_ids": []}
        return caps[cap_id]

    for j in jud.get("judgments", []):
        for cap in j.get("caps", []):
            entry = ensure(cap["cap_id"], cap.get("label"), cap.get("existing_id"))
            entry["example_ids"].extend(cap.get("members", []))

    # cappelli del seed DAG (sono id di Theme esistenti)
    for cap_id in cand.get("existing_caps", []):
        ensure(cap_id, None, cap_id)
    for e in cand.get("seed_edges", []):
        if e["generale"] in caps:
            caps[e["generale"]]["example_ids"].append(e["specifico"])

    # nomi degli esempi (max 6, unici)
    for entry in caps.values():
        seen, names = set(), []
        for mid in entry["example_ids"]:
            if mid in seen:
                continue
            seen.add(mid)
            names.append(themes.get(mid, {}).get("name") or mid)
            if len(names) >= 6:
                break
        entry["examples"] = names
    return caps


def build_orphans(judgments_path: Path, candidates_path: Path,
                  themes: dict[str, dict], cap_ids: set[str]) -> list[dict]:
    jud = json.loads(judgments_path.read_text(encoding="utf-8"))
    cand = json.loads(candidates_path.read_text(encoding="utf-8"))
    orphans: dict[str, dict] = {}

    def add(oid, source):
        if oid in cap_ids or oid in orphans:
            return
        info = themes.get(oid, {})
        orphans[oid] = {"orphan_id": oid, "name": info.get("name") or oid,
                        "description": info.get("description"), "source": source}

    for j in jud.get("judgments", []):
        for oid in j.get("unattached", []):
            add(oid, "unattached")
    for c in cand.get("clusters", []):
        if c["size"] == 1:
            add(c["members"][0]["id"], "singleton")
    return list(orphans.values())


# -----------------------------------------------------------------------------
# Embedding + shortlist
# -----------------------------------------------------------------------------

def embed(texts: list[str], model_name: str, device: Optional[str]) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("sentence-transformers non installato.") from exc
    model = SentenceTransformer(model_name, device=device)
    return model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                        show_progress_bar=False, batch_size=32).astype(np.float32)


def shortlists(orphans: list[dict], caps: list[dict], k: int,
               model_name: str, device: Optional[str]) -> list[tuple[list[str], float]]:
    cap_emb = embed([c["name"] for c in caps], model_name, device)
    orph_emb = embed([o["name"] for o in orphans], model_name, device)
    sims = orph_emb @ cap_emb.T               # coseno (vettori normalizzati)
    out = []
    kk = min(k, len(caps))
    for row in sims:
        idx = np.argsort(-row)[:kk]
        out.append(([caps[i]["cap_id"] for i in idx], float(row[idx[0]]) if kk else 0.0))
    return out


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


def _response_format(shortlist_ids: list[str]) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "orphan_assignment", "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "reasoning": {"type": "string"},
                    "decision": {"type": "string", "enum": ["specializes", "standalone"]},
                    "cap_id": {"type": ["string", "null"], "enum": [*shortlist_ids, None]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": ["reasoning", "decision", "cap_id", "confidence"],
                "additionalProperties": False,
            },
        },
    }


def build_user(orphan: dict, shortlist_caps: list[dict]) -> str:
    desc = (orphan.get("description") or "(nessuna descrizione)").replace("\n", " ")
    lines = [f"Tema orfano:\n- id: {orphan['orphan_id']} | nome: {orphan['name']}",
             f"  descrizione: {desc}", "", "Cappelli candidati:"]
    for c in shortlist_caps:
        ex = ", ".join(c["examples"][:6]) if c.get("examples") else "—"
        lines.append(f"- cap_id: {c['cap_id']} | nome: {c['name']}")
        lines.append(f"    esempi: {ex}")
    lines.append("\nL'orfano specializza uno di questi cappelli o è standalone? "
                 "Rispondi solo con l'oggetto JSON. /no_think")
    return "\n".join(lines)


def _extract_json(content: str) -> dict:
    s = (content or "").strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def _cache_key(orphan_id: str, shortlist_ids: list[str], model_id: str) -> str:
    raw = orphan_id + "||" + "|".join(shortlist_ids) + "||" + model_id + "||" + PROMPT_VERSION
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(judgments: Path, candidates: Path, graph: Path, out: Path, cache_path: Path,
        embed_model: str, model: Optional[str], url: str, k: int,
        temperature: float, seed: int, device: Optional[str],
        use_cache: bool, refresh: bool, dry_run: bool) -> Optional[HierarchyAssignments]:
    themes = _theme_index(graph)
    caps_map = build_caps(judgments, candidates, themes)
    caps = sorted(caps_map.values(), key=lambda c: c["cap_id"])
    orphans = build_orphans(judgments, candidates, themes, set(caps_map))
    if not caps:
        raise SystemExit("nessun cappello: serve prima il 5-3b")
    if not orphans:
        raise SystemExit("nessun orfano da agganciare")
    print(f"orfani: {len(orphans)} (unattached + singoletti)   cappelli: {len(caps)}   K={k}")

    sl = shortlists(orphans, caps, k, embed_model, device)

    if dry_run:
        print("\n--- DRY-RUN shortlist (nessuna chiamata Qwen) ---")
        for o, (ids, top) in zip(orphans, sl):
            names = " > ".join(caps_map[i]["name"] for i in ids)
            print(f"  {o['orphan_id']:<34} [{o['source'][:4]}] top={top:.2f}  ->  {names}")
        return None

    client = _make_client(url)
    model_id = _resolve_model(client, model, url)
    print(f"modello: {model_id}   prompt_version: {PROMPT_VERSION}")

    cache: dict = {}
    if use_cache and not refresh and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    assignments: list[OrphanAssignment] = []
    n_hits = 0
    for idx, (o, (ids, top)) in enumerate(zip(orphans, sl), 1):
        shortlist_caps = [caps_map[i] for i in ids]
        key = _cache_key(o["orphan_id"], ids, model_id)
        raw, from_cache = None, False
        if use_cache and not refresh and key in cache:
            raw, from_cache = cache[key]["result"], True
            n_hits += 1
        if raw is None:
            try:
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT},
                              {"role": "user", "content": build_user(o, shortlist_caps)}],
                    temperature=temperature, seed=seed, max_tokens=500,
                    response_format=_response_format(ids))
                raw = _extract_json(resp.choices[0].message.content)
            except Exception as exc:
                print(f"  [{o['orphan_id']}] errore: {exc}")
                continue
            if use_cache:
                cache[key] = {"orphan_id": o["orphan_id"], "shortlist": ids, "result": raw,
                              "timestamp": datetime.now(timezone.utc).isoformat()}
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

        decision = raw.get("decision", "standalone")
        cap_id = raw.get("cap_id")
        # validazione + fallback conservativo
        if decision == "specializes" and cap_id not in ids:
            decision, cap_id = "standalone", None
        if decision == "standalone":
            cap_id = None
        assignments.append(OrphanAssignment(
            orphan_id=o["orphan_id"], name=o["name"], source=o["source"],
            decision=decision, cap_id=cap_id, confidence=raw.get("confidence"),
            reasoning=raw.get("reasoning", ""), shortlist=ids, top_cosine=round(top, 4),
            from_cache=from_cache))
        if idx % 20 == 0:
            print(f"  [{idx}/{len(orphans)}] ...")

    report = HierarchyAssignments(
        source_judgments=str(judgments), source_candidates=str(candidates),
        source_graph=str(graph), timestamp=datetime.now(timezone.utc),
        model=model_id, embed_model=embed_model, k=k,
        n_orphans=len(orphans), n_caps=len(caps), assignments=assignments)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(report, caps_map, n_hits)
    return report


def _print_summary(r: HierarchyAssignments, caps_map: dict, n_hits: int) -> None:
    spec = [a for a in r.assignments if a.decision == "specializes"]
    standalone = [a for a in r.assignments if a.decision == "standalone"]
    per_cap: dict[str, int] = {}
    for a in spec:
        per_cap[a.cap_id] = per_cap.get(a.cap_id, 0) + 1
    print("\n--- riepilogo 5-3c ---")
    print(f"  orfani processati : {len(r.assignments)}   (cache hit: {n_hits})")
    print(f"  specializes       : {len(spec)}")
    print(f"  standalone        : {len(standalone)}")
    if per_cap:
        print("  agganci per cappello (top 12):")
        for cid, n in sorted(per_cap.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    {n:>2}x -> {cid}  ({caps_map[cid]['name']})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-3c fase 2: aggancio orfani ai cappelli.")
    ap.add_argument("--judgments", type=Path, default=DEFAULT_JUDGMENTS)
    ap.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--model-embed", default=DEFAULT_EMBED_MODEL, help="modello embedding (path locale bge-m3)")
    ap.add_argument("--model", default=None, help="modello Qwen (default: primo su LM Studio)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--k", type=int, default=DEFAULT_K, help="numero cappelli candidati per orfano")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="stampa solo le shortlist, niente Qwen")
    args = ap.parse_args()

    run(args.judgments, args.candidates, args.graph, args.out, args.cache,
        embed_model=args.model_embed, model=args.model, url=args.url, k=args.k,
        temperature=args.temperature, seed=args.seed, device=args.device,
        use_cache=not args.no_cache, refresh=args.refresh, dry_run=args.dry_run)


if __name__ == "__main__":
    main()