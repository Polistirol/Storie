# src/stage_5-3b_hierarchy_judge.py
"""
Stadio 5-3b — Giudizio gerarchico dei cluster (FASE 1 del 5-3b/c/d).

Per ogni cluster con >= 2 membri (dal 5-3a), chiede a Qwen (LM Studio) di
organizzarlo in una piccola gerarchia: fino a 2 CAPPELLI (il tema più generale,
preferibilmente un membro esistente), assegnando a ciascuno i membri che ne sono
una specializzazione, e lasciando in "unattached" gli outlier che il clustering
ha tirato dentro. Stabilisce così l'INSIEME DEI CAPPELLI su cui:
  - il 5-3c (fase 2) aggancerà per significato i singoletti e gli unattached;
  - il 5-3d poserà gli archi SPECIALIZES e i flag is_macro.

I singoletti (cluster size 1) sono SALTATI qui: nessuna gerarchia interna
possibile; verranno trattati nel 5-3c.

Rapporto con i refinement già giudicati (5-2b): i 15 archi del seed DAG sono
AUTORITATIVI e verranno posati direttamente dal 5-3d. Qui, per i cluster che ne
contengono entrambi gli estremi, li INIETTIAMO nel prompt come "già deciso, non
contraddire", così il giudizio dei cluster non genera struttura in conflitto.

Robustezza:
  - cache idempotente per cluster (chiave = membri ordinati + model + prompt_ver):
    i rilanci non rispendono chiamate; si rinormalizza/rivalida sempre (è gratis).
  - normalizzazione post-risposta: un cappello con members vuoto viene scartato e
    il suo eventuale existing_id spostato in unattached (rete per il bug del
    "cappello-fantasma" osservato in prova sul cluster #7).
  - validazione di partizione: ogni membro compare una volta sola.

Input  (default: data/stage_5/3_hierarchy/ e 2_themes/)
  - hierarchy_candidates.json (5-3a): cluster + seed_edges
  - enriched_graph.json       (5-2c): description dei Theme
Output (default: data/stage_5/3_hierarchy/)
  - hierarchy_judgments.json  (un record per cluster, consumato da 5-3c e 5-3d)
  - hierarchy_cache.json      (cache risposte grezze Qwen)

Uso:
  python -m src.stage_5-3b_hierarchy_judge --model qwen/qwen3-14b
  python -m src.stage_5-3b_hierarchy_judge --clusters 1 7   # solo alcuni (debug)
  python -m src.stage_5-3b_hierarchy_judge --refresh        # ignora la cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

PROMPT_VERSION = "0.2.0"
STAGE_VERSION = "0.1.0"
DEFAULT_URL = "http://localhost:1234/v1"

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
DEFAULT_HIER_DIR = _PROJECT_ROOT / "data" / "stage_5" / "3_hierarchy"
DEFAULT_CANDIDATES = DEFAULT_HIER_DIR / "hierarchy_candidates.json"
DEFAULT_GRAPH = _PROJECT_ROOT / "data" / "stage_5" / "2_themes" / "enriched_graph.json"
DEFAULT_CACHE = DEFAULT_HIER_DIR / "hierarchy_cache.json"
DEFAULT_OUT = DEFAULT_HIER_DIR / "hierarchy_judgments.json"


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Sei un organizzatore di temi per un knowledge graph biografico sulle "Memorie \
di Adriano" di Yourcenar. Ricevi un GRUPPO di temi (nodi Theme), ciascuno con \
id, nome e descrizione. Il gruppo è stato formato per vicinanza semantica, ma \
può essere coerente (un solo tema di fondo), misto (due temi di fondo) o \
incoerente (nessun tema condiviso).

Compito: organizzare il gruppo in una piccola gerarchia, scegliendo per ogni \
sotto-insieme un CAPPELLO (il tema più generale) e assegnandogli i membri che \
ne sono una declinazione più specifica.

Regole:
- AL MASSIMO 2 cappelli. Un solo tema di fondo -> 1 cappello; due temi mescolati \
(es. lutto E memoria) -> 2 cappelli; gruppo incoerente -> 0 cappelli e tutti i \
membri in "unattached".
- Un cappello DEVE avere almeno un membro nella sua lista "members". Un tema \
isolato, che non specializza nessun altro e non raccoglie nessun membro sotto di \
sé, NON va reso un cappello a sé: mettilo SOLO in "unattached", mai come cappello \
con members vuoto.
- PREFERISCI come cappello un membro ESISTENTE che sia già il tema generale \
(es. "Il corpo", "La morte", "Il lutto", "La memoria"): metti il suo id in \
"existing_id". Sintetizza un cappello NUOVO (existing_id=null, "label" breve e \
generale) solo se il tema generale NON è presente fra i membri.
- Per ogni cappello, "members" elenca gli id dei membri che ne sono una \
specializzazione. L'id del cappello stesso (existing_id) NON va nei suoi members.
- Ogni membro compare UNA sola volta: o è un cappello (existing_id), o sta nei \
members di un cappello, o sta in "unattached".
- NON forzare: i membri che non condividono il tema del gruppo (outlier tirati \
dentro dal raggruppamento) vanno in "unattached".
- Se ti vengono fornite RELAZIONI GIÀ DECISE, rispettale: NON contraddirle.
- Decidi in base alla DESCRIZIONE, non solo al nome.

Restituisci: "reasoning" (1-3 frasi sul tema/i di fondo e gli outlier), \
"coherence" ("coherent"|"mixed"|"incoherent"), "caps" (lista di {label, \
existing_id, members}), "unattached" (lista di id)."""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "theme_hierarchy",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "coherence": {"type": "string", "enum": ["coherent", "mixed", "incoherent"]},
                "caps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "existing_id": {"type": ["string", "null"]},
                            "members": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["label", "existing_id", "members"],
                        "additionalProperties": False,
                    },
                },
                "unattached": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["reasoning", "coherence", "caps", "unattached"],
            "additionalProperties": False,
        },
    },
}


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class CapJudgment(BaseModel):
    label: str
    cap_id: str                       # existing_id se esistente, altrimenti slug del label
    existing_id: Optional[str] = None # non-null => cappello da promuovere; null => nuovo da sintetizzare
    members: list[str] = Field(default_factory=list)


class ClusterJudgment(BaseModel):
    cluster_id: int
    size: int
    coherence: str
    reasoning: str
    caps: list[CapJudgment] = Field(default_factory=list)
    unattached: list[str] = Field(default_factory=list)
    injected_refinements: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    normalization_notes: list[str] = Field(default_factory=list)
    from_cache: bool = False


class HierarchyJudgments(BaseModel):
    source_candidates: str
    source_graph: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    prompt_version: str = PROMPT_VERSION
    model: str
    min_size: int
    n_clusters_judged: int
    judgments: list[ClusterJudgment] = Field(default_factory=list)


# from __future__ import annotations rende le annotazioni stringhe: forziamo la
# risoluzione dei forward-ref (Optional, modelli annidati) subito.
CapJudgment.model_rebuild()
ClusterJudgment.model_rebuild()
HierarchyJudgments.model_rebuild()


# -----------------------------------------------------------------------------
# IO / util
# -----------------------------------------------------------------------------

def slugify(label: str) -> str:
    s = unicodedata.normalize("NFKD", label)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s or "tema"


def build_clusters(candidates: Path, graph: Path, min_size: int,
                   only: Optional[list[int]]) -> tuple[list[dict], list[tuple[str, str, float]]]:
    cand = json.loads(candidates.read_text(encoding="utf-8"))
    g = json.loads(graph.read_text(encoding="utf-8"))
    desc_by_id = {n["id"]: n.get("description")
                  for n in g.get("nodes", []) if n.get("type") == "Theme"}
    seed_edges = [(e["specifico"], e["generale"], e.get("confidence"))
                  for e in cand.get("seed_edges", [])]
    clusters = []
    for c in cand.get("clusters", []):
        if c["size"] < min_size:
            continue
        if only is not None and c["cluster_id"] not in only:
            continue
        members = [{"id": m["id"], "name": m["name"], "freq": m["freq"],
                    "description": desc_by_id.get(m["id"])} for m in c["members"]]
        clusters.append({"cluster_id": c["cluster_id"], "size": c["size"],
                         "members": members})
    return clusters, seed_edges


def injected_for(cluster: dict, seed_edges: list[tuple[str, str, float]]) -> list[dict]:
    ids = {m["id"] for m in cluster["members"]}
    return [{"specifico": s, "generale": d, "confidence": c}
            for (s, d, c) in seed_edges if s in ids and d in ids]


def build_user(cluster: dict, injected: list[dict]) -> str:
    lines = [f"Gruppo di {len(cluster['members'])} temi:"]
    for m in cluster["members"]:
        desc = (m.get("description") or "(nessuna descrizione)").replace("\n", " ")
        lines.append(f"- id: {m['id']} | nome: {m['name']}")
        lines.append(f"  descrizione: {desc}")
    if injected:
        lines.append("\nRelazioni gerarchiche GIÀ DECISE (da rispettare, non contraddire):")
        for r in injected:
            lines.append(f"- {r['specifico']} specializza {r['generale']}")
    lines.append("\nOrganizza il gruppo secondo le regole. Rispondi solo con "
                 "l'oggetto JSON richiesto. /no_think")
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


def _make_client(url: str):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("openai non installato. pip install openai") from exc
    return OpenAI(base_url=url, api_key="lm-studio")


def _resolve_model(client, requested: Optional[str], url: str) -> str:
    if requested:
        return requested
    try:
        return client.models.list().data[0].id
    except Exception as exc:
        raise SystemExit(f"LM Studio non raggiungibile su {url}: {exc}") from exc


def _cache_key(member_ids: list[str], model_id: str) -> str:
    raw = "|".join(sorted(member_ids)) + "||" + model_id + "||" + PROMPT_VERSION
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Normalizzazione + validazione
# -----------------------------------------------------------------------------

def normalize(cluster: dict, result: dict) -> tuple[dict, list[str]]:
    """Rende l'output una PARTIZIONE garantita dei membri del cluster.

    Cura le patologie osservate:
    - un id che è cappello (existing_id) NON può stare anche nei members di un
      altro cappello -> rimosso dai members;
    - lo stesso membro in due cappelli -> tenuto nel primo, rimosso dagli altri;
    - cappello con members vuoto -> scartato, il suo existing_id torna disponibile;
    - cappelli duplicati con lo stesso existing_id (o stessa label, se nuovi) ->
      fusi;
    - membri dimenticati dal modello -> raccolti in unattached.

    Post-condizione: ogni membro compare una sola volta (cappello | un members |
    unattached). Ritorna (result_normalizzato, note).
    """
    notes: list[str] = []
    member_ids = {m["id"] for m in cluster["members"]}

    # Pass A — prefiltro members validi + fusione di cappelli duplicati.
    merged: dict = {}   # key -> {label, existing_id, members}
    order: list = []
    for cap in result.get("caps", []):
        eid = cap.get("existing_id")
        eid = eid if (eid in member_ids) else None
        key = eid if eid is not None else ("__new__", (cap.get("label") or "").strip().lower())
        mem = [mid for mid in cap.get("members", []) if mid in member_ids]
        if key not in merged:
            merged[key] = {"label": cap.get("label", ""), "existing_id": eid, "members": []}
            order.append(key)
        else:
            notes.append(f"cappello duplicato «{cap.get('label')}» fuso")
        merged[key]["members"].extend(mem)

    # Pass B — cappelli sopravvissuti = quelli con >=1 membro diverso dal proprio eid.
    cap_eid_set: set[str] = set()
    survivors = []
    for key in order:
        cap = merged[key]
        mem = list(dict.fromkeys(m for m in cap["members"] if m != cap["existing_id"]))
        if mem:
            survivors.append({"label": cap["label"], "existing_id": cap["existing_id"], "members": mem})
            if cap["existing_id"]:
                cap_eid_set.add(cap["existing_id"])
        else:
            notes.append(f"cappello vuoto «{cap['label']}» scartato")

    # Pass C — dedup cross-cappello e rimozione di id che sono essi stessi cappelli.
    kept = []
    globally: set[str] = set(cap_eid_set)
    for cap in survivors:
        mem = []
        for mid in cap["members"]:
            if mid in cap_eid_set:
                notes.append(f"{mid!r} è un cappello: tolto dai members di «{cap['label']}»")
                continue
            if mid in globally:
                notes.append(f"{mid!r} già assegnato: tolto dai members di «{cap['label']}»")
                continue
            mem.append(mid)
            globally.add(mid)
        if mem:
            kept.append({"label": cap["label"], "existing_id": cap["existing_id"], "members": mem})
        else:
            notes.append(f"cappello «{cap['label']}» svuotato dopo dedup, scartato")

    # Pass D — unattached = dichiarati + dimenticati, meno gli assegnati.
    assigned: set[str] = set()
    for cap in kept:
        if cap["existing_id"]:
            assigned.add(cap["existing_id"])
        assigned.update(cap["members"])
    unattached = list(result.get("unattached", []))
    forgotten = [mid for mid in member_ids if mid not in assigned and mid not in unattached]
    if forgotten:
        notes.append(f"non assegnati dal modello -> unattached: {forgotten}")
    unattached = [u for u in dict.fromkeys(unattached + forgotten)
                  if u in member_ids and u not in assigned]

    return {"reasoning": result.get("reasoning", ""),
            "coherence": result.get("coherence", ""),
            "caps": kept, "unattached": unattached}, notes


def validate(cluster: dict, result: dict) -> list[str]:
    warns: list[str] = []
    member_ids = {m["id"] for m in cluster["members"]}
    caps = result.get("caps", [])
    if len(caps) > 2:
        warns.append(f"più di 2 cappelli ({len(caps)})")
    seen: dict[str, int] = {}
    for cap in caps:
        eid = cap.get("existing_id")
        if eid is not None:
            if eid not in member_ids:
                warns.append(f"existing_id {eid!r} non è un membro")
            seen[eid] = seen.get(eid, 0) + 1
        for mid in cap.get("members", []):
            seen[mid] = seen.get(mid, 0) + 1
    for mid in result.get("unattached", []):
        seen[mid] = seen.get(mid, 0) + 1
    dups = [k for k, v in seen.items() if v > 1]
    if dups:
        warns.append(f"assegnati più volte: {dups}")
    missing = sorted(member_ids - set(seen))
    if missing:
        warns.append(f"non assegnati: {missing}")
    return warns


def finalize_caps(result: dict, existing_theme_ids: set[str]) -> list[CapJudgment]:
    """Assegna cap_id (existing_id o slug del label) e segnala collisioni di slug."""
    caps = []
    used_ids: set[str] = set()
    for cap in result["caps"]:
        eid = cap.get("existing_id")
        if eid:
            cap_id = eid
        else:
            cap_id = slugify(cap["label"])
            # disambigua collisioni con id esistenti o con altri nuovi cappelli
            base = cap_id
            k = 2
            while cap_id in existing_theme_ids or cap_id in used_ids:
                cap_id = f"{base}_{k}"
                k += 1
        used_ids.add(cap_id)
        caps.append(CapJudgment(label=cap["label"], cap_id=cap_id,
                                existing_id=eid, members=cap["members"]))
    return caps


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(candidates: Path, graph: Path, out: Path, cache_path: Path,
        model: Optional[str], url: str, temperature: float, seed: int,
        min_size: int, only: Optional[list[int]], use_cache: bool, refresh: bool) -> HierarchyJudgments:
    clusters, seed_edges = build_clusters(candidates, graph, min_size, only)
    if not clusters:
        raise SystemExit("nessun cluster da giudicare (controlla min-size/clusters)")

    g = json.loads(graph.read_text(encoding="utf-8"))
    existing_theme_ids = {n["id"] for n in g.get("nodes", []) if n.get("type") == "Theme"}

    client = _make_client(url)
    model_id = _resolve_model(client, model, url)
    print(f"modello: {model_id}   prompt_version: {PROMPT_VERSION}   cluster: {len(clusters)}")

    cache: dict = {}
    if use_cache and not refresh and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    judgments: list[ClusterJudgment] = []
    n_hits = 0
    for idx, cluster in enumerate(clusters, 1):
        member_ids = [m["id"] for m in cluster["members"]]
        injected = injected_for(cluster, seed_edges)
        key = _cache_key(member_ids, model_id)
        from_cache = False
        raw = None
        if use_cache and not refresh and key in cache:
            raw = cache[key]["result"]
            from_cache = True
            n_hits += 1
        if raw is None:
            try:
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT},
                              {"role": "user", "content": build_user(cluster, injected)}],
                    temperature=temperature, seed=seed, max_tokens=1100,
                    response_format=RESPONSE_FORMAT)
                raw = _extract_json(resp.choices[0].message.content)
            except Exception as exc:
                print(f"  [#{cluster['cluster_id']}] errore: {exc}")
                continue
            if use_cache:
                cache[key] = {"cluster_id": cluster["cluster_id"], "member_ids": member_ids,
                              "result": raw, "timestamp": datetime.now(timezone.utc).isoformat()}
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

        norm, notes = normalize(cluster, raw)
        warns = validate(cluster, norm)
        caps = finalize_caps(norm, existing_theme_ids)
        judgments.append(ClusterJudgment(
            cluster_id=cluster["cluster_id"], size=cluster["size"],
            coherence=norm["coherence"], reasoning=norm["reasoning"],
            caps=caps, unattached=norm["unattached"],
            injected_refinements=injected, warnings=warns,
            normalization_notes=notes, from_cache=from_cache))
        tag = "cache" if from_cache else "  LLM"
        flag = " ⚠" if warns else ""
        print(f"  [{idx}/{len(clusters)}] {tag} #{cluster['cluster_id']:>3} "
              f"n={cluster['size']:>2} {norm['coherence']:<10} "
              f"caps={len(caps)} unatt={len(norm['unattached'])}{flag}")

    report = HierarchyJudgments(
        source_candidates=str(candidates), source_graph=str(graph),
        timestamp=datetime.now(timezone.utc), model=model_id, min_size=min_size,
        n_clusters_judged=len(judgments), judgments=judgments)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(report, n_hits)
    return report


def _print_summary(r: HierarchyJudgments, n_hits: int) -> None:
    coh = {"coherent": 0, "mixed": 0, "incoherent": 0}
    n_caps_new = n_caps_existing = n_unatt = n_warn = 0
    for j in r.judgments:
        coh[j.coherence] = coh.get(j.coherence, 0) + 1
        for c in j.caps:
            if c.existing_id:
                n_caps_existing += 1
            else:
                n_caps_new += 1
        n_unatt += len(j.unattached)
        if j.warnings:
            n_warn += 1
    print("\n--- riepilogo 5-3b ---")
    print(f"  cluster giudicati : {r.n_clusters_judged}   (cache hit: {n_hits})")
    print(f"  coerenza          : coherent={coh['coherent']} mixed={coh['mixed']} incoherent={coh['incoherent']}")
    print(f"  cappelli          : esistenti(da promuovere)={n_caps_existing}  nuovi(da sintetizzare)={n_caps_new}")
    print(f"  membri unattached : {n_unatt}   (andranno al 5-3c con i singoletti)")
    print(f"  cluster con warning: {n_warn}")
    if n_warn:
        print("  rivedi i 'warnings' nei record con problemi:")
        for j in r.judgments:
            if j.warnings:
                print(f"    #{j.cluster_id}: {j.warnings}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-3b fase 1: giudizio gerarchico dei cluster.")
    ap.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--model", default=None, help="es. qwen/qwen3-14b (default: primo modello LM Studio)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-size", type=int, default=2, help="salta i cluster sotto questa dimensione (default 2)")
    ap.add_argument("--clusters", type=int, nargs="+", default=None, help="solo questi cluster_id (debug)")
    ap.add_argument("--no-cache", action="store_true", help="non leggere né scrivere cache")
    ap.add_argument("--refresh", action="store_true", help="ignora la cache esistente e richiama Qwen")
    args = ap.parse_args()

    run(args.candidates, args.graph, args.out, args.cache,
        model=args.model, url=args.url, temperature=args.temperature, seed=args.seed,
        min_size=args.min_size, only=args.clusters,
        use_cache=not args.no_cache, refresh=args.refresh)


if __name__ == "__main__":
    main()