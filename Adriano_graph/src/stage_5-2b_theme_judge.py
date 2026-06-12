# src/stage_5-2b_theme_judge.py
"""
Stadio 5-2b — Giudizio LLM sulle coppie candidate Theme.

Secondo pezzo del consolidamento Theme (5-2). Fa giudicare a un LLM locale
(Qwen3-14B via LM Studio) la relazione fra le coppie candidate prodotte dal
5-2a. NON applica merge al grafo: produce solo i giudizi. Il merge lo fa il 5-2c.

Verdetti (3 vie)
----------------
- "same"       : stesso identico tema, solo parole diverse  -> diventerà merge nel 5-2c.
- "refinement" : stessa famiglia, uno più specifico dell'altro -> NON si fonde
                 (lo schema non ha un arco di gerarchia): si registra e basta.
                 `general_id` = id del tema più generale.
- "distinct"   : temi diversi, anche se i nomi si somigliano.
Bias esplicito verso "distinct": fondere temi distinti distrugge informazione.

Input  (default: data/stage_5/2_themes/theme_candidates.json — output 5-2a)
Output (default: data/stage_5/2_themes/theme_judgments.json)

Bande
-----
Giudica solo le coppie nelle bande richieste (`--bands`, default `auto,judge`);
la `drop` viene ignorata. Sulle ~37 coppie attese (4 auto + 33 judge) il batch
gira in secondi. Il campo `band` è letto dal candidato; se assente viene
ricalcolato dalle soglie di default.

Idempotenza / cache
-------------------
Se `theme_judgments.json` esiste e non c'è `--refresh`, i giudizi già presenti
con la stessa chiave (theme_a, theme_b, model, prompt_version) vengono RIUSATI:
l'LLM è chiamato solo per le coppie nuove o se cambia modello/prompt. Rilanciare
non rispende inferenze.

LM Studio
---------
Endpoint OpenAI-compatibile (default http://localhost:1234/v1). Carica
Qwen3-14B (Q4_K_M) e avvia il server locale dal tab Developer PRIMA di lanciare.
Output vincolato via response_format json_schema (strict). Thinking OFF
(`/no_think` + schema vincolato).
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

PROMPT_VERSION = "0.2.0"
STAGE_VERSION = "0.1.0"

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_THEMES_DIR = _PROJECT_ROOT / "data" / "stage_5" / "2_themes"
DEFAULT_CANDIDATES = _THEMES_DIR / "theme_candidates.json"
DEFAULT_OUT = _THEMES_DIR / "theme_judgments.json"

DEFAULT_URL = "http://localhost:1234/v1"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_BANDS = "auto,judge"

# Soglie di banda (fallback se il candidato non porta già il campo `band`).
_AUTO_BAND_COS = 0.92
_JUDGE_BAND_COS = 0.80
_STRONG_JACCARD = 0.60


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Sei un giudice di equivalenza tematica per un knowledge graph biografico \
costruito sulle "Memorie di Adriano" di Yourcenar. Ricevi DUE temi (nodi \
Theme), ciascuno con un nome canonico e una descrizione. Stabilisci se \
denotano lo STESSO tema astratto, scegliendo UNO di tre verdetti.

COSA È UN TEMA. Un tema è un'IDEA ASTRATTA ricorrente (la morte, il potere, la \
memoria, il corpo, il lutto...), non un episodio. Lo stesso tema compare in \
PUNTI DIVERSI del libro: per questo le due descrizioni che ricevi si \
riferiscono quasi sempre a scene, oggetti o persone DIVERSE. Questo è NORMALE \
e ATTESO, e NON rende i due temi distinti. Le descrizioni servono a capire \
COSA SIGNIFICA ciascun nome, non a pretendere che raccontino lo stesso momento.

VERDETTI
- "same": denotano la STESSA idea astratta, anche se illustrata da scene \
diverse o formulata con parole diverse. Vanno fusi. Esempi (descrizioni \
diverse, stesso tema):
  · "Anima e corpo" / "Corpo e anima" — stessa dualità, anche se un chunk \
parla di rottura e l'altro di assimilazione.
  · "Divinizzazione di Adriano" / "Divinizzazione dell'imperatore" — \
l'imperatore È Adriano.
  · due id identici a meno del suffisso "__theme"/"__phase" (es. \
"lutto_per_antinoo" e "lutto_per_antinoo__theme") sono SEMPRE lo stesso tema.
- "refinement": stessa famiglia, ma uno è un caso PIÙ SPECIFICO dell'altro. \
NON si fonde (è struttura tematica fine). "general_id" = id del tema più \
GENERALE. Es: "Sete di potere" (generale) / "Sete di potere e gloria" \
(specifico).
- "distinct": idee DIVERSE, anche se i nomi condividono una parola astratta. \
Es: "Potere e successione" / "Potere e moderazione" condividono "potere" ma \
sono idee diverse; "Guerra e politica" / "Guerra e violenza" idem.

COME DECIDERE (due passi):
1. Ignora le scene specifiche e chiediti: i due nomi indicano la stessa idea \
astratta? Se l'idea coincide → "same".
2. Usa la descrizione SOLO per disambiguare un nome vago (capire quale idea \
intende), MAI per cercare differenze di scena.

BIAS: scegli "distinct" solo quando le IDEE sono davvero diverse, non quando \
differiscono le scene o i dettagli. Tra "same" e "refinement" in dubbio scegli \
"refinement". Non inventare distinzioni dal fatto che le descrizioni citano \
episodi diversi.

Restituisci: "reasoning" (1-2 frasi sul perché le IDEE coincidono o \
differiscono), poi "verdict", "confidence" (0.0-1.0), "general_id" (id del \
tema più generale se verdict="refinement", altrimenti null)."""

USER_TEMPLATE = """\
Tema A:
  id: {id_a}
  nome: {name_a}
  descrizione: {desc_a}

Tema B:
  id: {id_b}
  nome: {name_b}
  descrizione: {desc_b}

Decidi la relazione fra Tema A e Tema B secondo le regole. Rispondi solo con \
l'oggetto JSON richiesto. /no_think"""

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "theme_relation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "verdict": {"type": "string", "enum": ["same", "refinement", "distinct"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "general_id": {"type": ["string", "null"]},
            },
            "required": ["reasoning", "verdict", "confidence", "general_id"],
            "additionalProperties": False,
        },
    },
}


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class Judgment(BaseModel):
    theme_a: str
    theme_b: str
    name_a: str
    name_b: str
    band: str
    cosine: Optional[float] = None
    jaccard: Optional[float] = None
    verdict: str  # same | refinement | distinct | error
    confidence: Optional[float] = None
    general_id: Optional[str] = None
    reasoning: Optional[str] = None
    review_needed: bool = False
    note: Optional[str] = None
    model: str
    prompt_version: str = PROMPT_VERSION
    judged_at: datetime


class JudgmentReport(BaseModel):
    source_candidates: str
    model: str
    prompt_version: str = PROMPT_VERSION
    stage_version: str = STAGE_VERSION
    lmstudio_url: str
    temperature: float
    bands: list[str]
    timestamp: datetime
    n_judged: int
    counts: dict[str, int] = Field(default_factory=dict)
    judgments: list[Judgment] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Lettura candidati
# -----------------------------------------------------------------------------

def _derive_band(cosine: Optional[float], jaccard: Optional[float]) -> str:
    if cosine is not None and cosine >= _AUTO_BAND_COS:
        return "auto"
    if (cosine is not None and cosine >= _JUDGE_BAND_COS) or \
       (jaccard is not None and jaccard >= _STRONG_JACCARD):
        return "judge"
    return "drop"


def load_candidates(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    out: list[dict] = []
    for c in data.get("candidates", []):
        band = c.get("band") or _derive_band(c.get("cosine"), c.get("jaccard"))
        out.append({
            "theme_a": c["theme_a"], "theme_b": c["theme_b"],
            "name_a": c["name_a"], "name_b": c["name_b"],
            "desc_a": c.get("desc_a"), "desc_b": c.get("desc_b"),
            "cosine": c.get("cosine"), "jaccard": c.get("jaccard"),
            "band": band,
        })
    return out


# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------

def _cache_key(theme_a: str, theme_b: str, model: str, prompt_version: str) -> tuple:
    return (theme_a, theme_b, model, prompt_version)


def load_cache(path: Path) -> dict[tuple, Judgment]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cache: dict[tuple, Judgment] = {}
    for j in data.get("judgments", []):
        try:
            jud = Judgment(**j)
        except Exception:
            continue
        cache[_cache_key(jud.theme_a, jud.theme_b, jud.model, jud.prompt_version)] = jud
    return cache


# -----------------------------------------------------------------------------
# LLM (LM Studio, OpenAI-compatibile)
# -----------------------------------------------------------------------------

def _make_client(url: str):
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("openai non installato. Installa con: pip install openai") from exc
    return OpenAI(base_url=url, api_key="lm-studio")


def resolve_model_id(client, requested: Optional[str], url: str) -> str:
    if requested:
        return requested
    try:
        models = client.models.list()
        ids = [m.id for m in models.data]
    except Exception as exc:
        raise SystemExit(
            f"LM Studio non raggiungibile su {url} (GET /models fallita: {exc}). "
            "Carica il modello e avvia il server locale dal tab Developer."
        ) from exc
    if not ids:
        raise SystemExit(f"nessun modello caricato in LM Studio su {url}.")
    return ids[0]


def _extract_json(content: str) -> dict:
    s = (content or "").strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def _call_llm(client, model_id: str, temperature: float, seed: int,
              id_a: str, name_a: str, desc_a: str,
              id_b: str, name_b: str, desc_b: str) -> dict:
    user = USER_TEMPLATE.format(
        id_a=id_a, name_a=name_a, desc_a=desc_a or "(nessuna descrizione)",
        id_b=id_b, name_b=name_b, desc_b=desc_b or "(nessuna descrizione)",
    )
    resp = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        seed=seed,
        max_tokens=512,
        response_format=RESPONSE_FORMAT,
    )
    return _extract_json(resp.choices[0].message.content)


def judge_pair(client, model_id, temperature, seed, cand: dict, attempts: int = 3) -> Judgment:
    id_a, id_b = cand["theme_a"], cand["theme_b"]
    now = datetime.now(timezone.utc)
    last_err: Optional[Exception] = None

    for _ in range(attempts):
        try:
            data = _call_llm(
                client, model_id, temperature, seed,
                id_a, cand["name_a"], cand["desc_a"],
                id_b, cand["name_b"], cand["desc_b"],
            )
            verdict = data.get("verdict")
            conf = data.get("confidence")
            gid = data.get("general_id")
            review, note = False, None

            if verdict not in ("same", "refinement", "distinct"):
                return Judgment(
                    theme_a=id_a, theme_b=id_b, name_a=cand["name_a"], name_b=cand["name_b"],
                    band=cand["band"], cosine=cand["cosine"], jaccard=cand["jaccard"],
                    verdict="error", confidence=None, general_id=None,
                    reasoning=data.get("reasoning"), review_needed=True,
                    note=f"verdict non valido: {verdict!r}",
                    model=model_id, judged_at=now,
                )

            if verdict == "refinement":
                if gid not in (id_a, id_b):
                    review, note, gid = True, f"general_id non valido: {gid!r}", None
            else:  # same | distinct
                gid = None

            return Judgment(
                theme_a=id_a, theme_b=id_b, name_a=cand["name_a"], name_b=cand["name_b"],
                band=cand["band"], cosine=cand["cosine"], jaccard=cand["jaccard"],
                verdict=verdict, confidence=conf, general_id=gid,
                reasoning=data.get("reasoning"), review_needed=review, note=note,
                model=model_id, judged_at=now,
            )
        except Exception as exc:
            last_err = exc

    return Judgment(
        theme_a=id_a, theme_b=id_b, name_a=cand["name_a"], name_b=cand["name_b"],
        band=cand["band"], cosine=cand["cosine"], jaccard=cand["jaccard"],
        verdict="error", confidence=None, general_id=None,
        reasoning=None, review_needed=True, note=f"chiamata fallita: {last_err}",
        model=model_id, judged_at=now,
    )


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def run(
    candidates_path: Path,
    out_path: Path,
    bands: set[str],
    url: str = DEFAULT_URL,
    model: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    seed: int = 0,
    refresh: bool = False,
    dry_run: bool = False,
) -> JudgmentReport:
    candidates = load_candidates(candidates_path)
    targets = [c for c in candidates if c["band"] in bands]

    if dry_run:
        print(f"[DRY-RUN] candidati totali: {len(candidates)}  "
              f"da giudicare (bande {sorted(bands)}): {len(targets)}")
        for c in targets:
            cs = f"{c['cosine']:.3f}" if c["cosine"] is not None else "  -  "
            print(f"   [{c['band']}] cos={cs}  {c['theme_a']}  ~  {c['theme_b']}")
        return JudgmentReport(
            source_candidates=str(candidates_path), model=model or "(non risolto)",
            lmstudio_url=url, temperature=temperature, bands=sorted(bands),
            timestamp=datetime.now(timezone.utc), n_judged=0, judgments=[],
        )

    client = _make_client(url)
    model_id = resolve_model_id(client, model, url)
    cache = {} if refresh else load_cache(out_path)

    judgments: list[Judgment] = []
    reused = 0
    for c in targets:
        key = _cache_key(c["theme_a"], c["theme_b"], model_id, PROMPT_VERSION)
        if key in cache:
            judgments.append(cache[key])
            reused += 1
        else:
            judgments.append(judge_pair(client, model_id, temperature, seed, c))

    # ordina: same -> refinement -> distinct -> error, poi confidence decrescente
    order = {"same": 0, "refinement": 1, "distinct": 2, "error": 3}
    judgments.sort(key=lambda j: (order.get(j.verdict, 9), -(j.confidence or 0.0)))

    counts = {v: sum(1 for j in judgments if j.verdict == v)
              for v in ("same", "refinement", "distinct", "error")}
    report = JudgmentReport(
        source_candidates=str(candidates_path), model=model_id,
        lmstudio_url=url, temperature=temperature, bands=sorted(bands),
        timestamp=datetime.now(timezone.utc), n_judged=len(judgments),
        counts=counts, judgments=judgments,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    _print_summary(report, reused)
    return report


def _print_summary(report: JudgmentReport, reused: int) -> None:
    print(f"stage 5-2b — giudizio Theme  (modello {report.model}, prompt {report.prompt_version})")
    print(f"  coppie giudicate : {report.n_judged}  (riusate da cache: {reused})")
    c = report.counts
    print(f"  verdetti         : same={c['same']}  refinement={c['refinement']}  "
          f"distinct={c['distinct']}  error={c['error']}")
    same = [j for j in report.judgments if j.verdict == "same"]
    if same:
        print("  --- SAME (candidati a merge nel 5-2c) ---")
        for j in same:
            cs = f"{j.confidence:.2f}" if j.confidence is not None else " - "
            print(f"   conf={cs}  {j.theme_a}  ==  {j.theme_b}")
            if j.reasoning:
                print(f"        {j.reasoning[:160]}")
    ref = [j for j in report.judgments if j.verdict == "refinement"]
    if ref:
        print("  --- REFINEMENT (registrati, NON fusi) ---")
        for j in ref:
            print(f"   generale={j.general_id}  {j.theme_a}  >  {j.theme_b}")
    err = [j for j in report.judgments if j.verdict == "error"]
    if err:
        print(f"  --- ERROR ({len(err)}): rivedere/rilanciare ---")
        for j in err:
            print(f"   {j.theme_a} ~ {j.theme_b}  [{j.note}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-2b: giudizio LLM sulle coppie candidate Theme.")
    ap.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES, help="theme_candidates.json (output 5-2a)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="theme_judgments.json")
    ap.add_argument("--bands", default=DEFAULT_BANDS, help="bande da giudicare, separate da virgola (default auto,judge)")
    ap.add_argument("--url", default=DEFAULT_URL, help="base URL del server LM Studio")
    ap.add_argument("--model", default=None, help="id modello (se assente usa quello caricato in LM Studio)")
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--refresh", action="store_true", help="ignora la cache e ri-giudica tutto")
    ap.add_argument("--dry-run", action="store_true", help="non chiama l'API: elenca le coppie che verrebbero giudicate")
    args = ap.parse_args()

    bands = {b.strip() for b in args.bands.split(",") if b.strip()}
    run(
        args.candidates, args.out, bands,
        url=args.url, model=args.model, temperature=args.temperature,
        seed=args.seed, refresh=args.refresh, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()