# src/stage_5-2a_theme_candidates.py
"""
Stadio 5-2a — Generatore di candidati per il consolidamento Theme.

Primo pezzo del 5-2. DETERMINISTICO, NIENTE giudizi di merge: prende i nodi
Theme dal grafo arricchito e produce un elenco ispezionabile di COPPIE
CANDIDATE (temi potenzialmente da consolidare). Il giudizio "stessi/distinti"
lo fa Qwen al 5-2b sulle sole coppie qui surfate; il merge lo applica il 5-2c.

Perché due canali
-----------------
I Theme di questo grafo non sono varianti lessicali pulite (`bellezza` vs
`bellezza_e_virtu`): sono temi compositi che spesso NON condividono token pur
essendo vicini (`limite_e_vecchiaia` ~ `perdita_delle_capacita_fisiche`).
Quindi:
- canale DENSO (portante): similarità coseno fra embedding BGE-M3 dei `name`.
- canale LESSICALE (recall residuo): Jaccard fra i token di id/name, per non
  perdere i pochi casi di vera variante lessicale.
L'unione dei due canali è l'insieme dei candidati.

Embedding del `name`, non della `description`
---------------------------------------------
Si embedda il `name` (la forma canonica del concetto). La `description` è
specifica del chunk e introdurrebbe rumore nel decidere se due temi sono lo
STESSO tema generale. La description viene comunque riportata nell'output, e
sarà Qwen al 5-2b a leggerla per il giudizio fine. Flag `--embed name_desc`
per provare l'alternativa.

Input  (default: data/stage_5/1_embodies/enriched_graph.json)
Output (default: data/stage_5/2_themes/)
- theme_candidates.json  (header + lista di CandidatePair, autoritativo)
- theme_candidates.tsv    (stessa info, per ispezione rapida in foglio di calcolo)

Questo modulo NON modifica il grafo e NON decide nulla: sola lettura + analisi.
Le soglie di default sono volutamente BASSE (recall): l'idea è guardare la
distribuzione dei coseni stampata a fine run e poi, se serve, rilanciare con
soglie tarate prima di passare a Qwen.

USAGE:
default using cuda e default dir:
python -m src.stage_5-2a_theme_candidates

custom model and device:
python -m src.stage_5-2a_theme_candidates --model D:\models\ bge-m3 --device cuda

"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

# numpy è l'unica dipendenza pesante oltre al modello di embedding.
import numpy as np

STAGE_VERSION = "0.1.0"
DEFAULT_MODEL = r"C:\Users\Pc-Gaming\Documents\models\embeddings\bge-m3"

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
DEFAULT_INPUT = _PROJECT_ROOT / "data" / "stage_5" / "1_embodies" / "enriched_graph.json"
DEFAULT_OUT_DIR = _PROJECT_ROOT / "data" / "stage_5" / "2_themes"

# Soglie di default: basse, orientate al recall. Da tarare dopo aver visto la
# distribuzione dei coseni.
DEFAULT_TOP_K = 8          # vicini densi per tema
DEFAULT_COSINE_GATE = 0.60 # sotto questo coseno, scarta la coppia densa
DEFAULT_JACCARD_GATE = 0.30 # Jaccard minimo per il canale lessicale (1 token comune su ~3)

# Soglie bande diagnostiche (non filtrano i candidati, solo annotazione/report).
DEFAULT_AUTO_BAND_COS = 0.92
DEFAULT_JUDGE_BAND_COS = 0.80
DEFAULT_STRONG_JACCARD = 0.60

# Stopword italiane "di servizio" che non portano significato tematico: vanno
# tolte dai token prima del Jaccard, altrimenti due temi compositi sembrano
# simili solo perché entrambi contengono "e"/"di"/"come".
_STOPWORDS: frozenset[str] = frozenset({
    "e", "ed", "o", "di", "del", "dei", "della", "delle", "degli", "lo", "la",
    "il", "i", "le", "gli", "un", "uno", "una", "come", "con", "a", "ad", "al",
    "ai", "alla", "alle", "da", "dal", "in", "nel", "nella", "su", "sul", "per",
    "tra", "fra", "che", "non", "se",
})

# Suffisso da split di tipo (stadio 4): `..._theme`, `..._event`, ecc.
_SPLIT_SUFFIX_RE = re.compile(
    r"__(event|theme|phase|person|place|work|era|reflection)$", re.IGNORECASE
)


# -----------------------------------------------------------------------------
# Modelli output
# -----------------------------------------------------------------------------

class CandidatePair(BaseModel):
    theme_a: str               # id (ordinato: theme_a < theme_b)
    theme_b: str
    name_a: str
    name_b: str
    cosine: Optional[float] = None
    jaccard: Optional[float] = None
    channels: list[str] = Field(default_factory=list)  # ["dense"], ["lexical"], o entrambi
    freq_a: int = 0            # n. provenances (proxy di frequenza/canonicità d'uso)
    freq_b: int = 0
    desc_a: Optional[str] = None
    desc_b: Optional[str] = None
    band: str = "drop"         # auto | judge | drop (annotazione diagnostica)


class CandidateReport(BaseModel):
    source_graph: str
    timestamp: datetime
    stage_version: str = STAGE_VERSION
    model: str
    embed_text: str            # "name" | "name_desc"
    top_k: int
    cosine_gate: float
    jaccard_gate: float
    n_themes: int
    n_candidates: int
    n_dense_only: int
    n_lexical_only: int
    n_both: int
    cosine_percentiles: dict[str, float]   # distribuzione dei coseni dei vicini top-1
    top_hub_themes: list[dict]             # temi con più candidati (possibili over-connessi)
    band_counts: dict[str, int] = Field(default_factory=dict)
    band_thresholds: dict[str, float] = Field(default_factory=dict)
    candidates: list[CandidatePair] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Helper deterministici (testabili senza modello)
# -----------------------------------------------------------------------------

def strip_split_suffix(theme_id: str) -> str:
    """Rimuove l'eventuale suffisso di split di tipo (`..._theme`)."""
    return _SPLIT_SUFFIX_RE.sub("", theme_id)


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def tokenize(theme_id: str, name: str) -> frozenset[str]:
    """
    Token significativi di un tema, da id (split su `_`) e name (split su
    non-alfanumerici), in minuscolo, senza accenti, senza stopword.
    """
    raw_id = strip_split_suffix(theme_id)
    parts = re.split(r"[^0-9a-zA-Z]+", _strip_accents(raw_id + " " + name).lower())
    return frozenset(p for p in parts if p and p not in _STOPWORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def classify_band(
    pair: CandidatePair,
    *,
    auto_band_cos: float,
    judge_band_cos: float,
    strong_jaccard: float,
) -> str:
    """Classifica una coppia in esattamente una banda diagnostica (auto/judge/drop)."""
    if pair.cosine is not None and pair.cosine >= auto_band_cos:
        return "auto"
    strong_lex = pair.jaccard is not None and pair.jaccard >= strong_jaccard
    in_judge_cos = (
        pair.cosine is not None
        and judge_band_cos <= pair.cosine < auto_band_cos
    )
    if in_judge_cos or strong_lex:
        return "judge"
    return "drop"


def annotate_bands(
    candidates: list[CandidatePair],
    *,
    auto_band_cos: float,
    judge_band_cos: float,
    strong_jaccard: float,
) -> dict[str, int]:
    counts = {"auto": 0, "judge": 0, "drop": 0}
    for cp in candidates:
        cp.band = classify_band(
            cp,
            auto_band_cos=auto_band_cos,
            judge_band_cos=judge_band_cos,
            strong_jaccard=strong_jaccard,
        )
        counts[cp.band] += 1
    return counts


def _trunc_text(s: Optional[str], n: int = 100) -> str:
    if not s:
        return ""
    flat = " ".join(s.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _cosine_histogram_bins(candidates: list[CandidatePair]) -> list[tuple[str, int]]:
    """Bin del coseno [0.60, 1.00] a passo 0.05; solo coppie con coseno noto."""
    edges = [0.60 + i * 0.05 for i in range(9)]  # 0.60 … 1.00
    counts = [0] * 8
    for c in candidates:
        if c.cosine is None or c.cosine < 0.60:
            continue
        cos = min(c.cosine, 1.0)
        idx = min(int((cos - 0.60) / 0.05), 7)
        counts[idx] += 1
    labels = [
        f"{edges[i]:.2f}-{edges[i + 1]:.2f}" for i in range(8)
    ]
    return list(zip(labels, counts))


# -----------------------------------------------------------------------------
# Lettura grafo
# -----------------------------------------------------------------------------

class _Theme(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    freq: int


def load_themes(graph_path: Path) -> list[_Theme]:
    with graph_path.open("r", encoding="utf-8") as f:
        graph = json.load(f)
    themes: list[_Theme] = []
    for n in graph.get("nodes", []):
        if n.get("type") == "Theme":
            themes.append(_Theme(
                id=n["id"],
                name=n["name"],
                description=n.get("description"),
                freq=len(n.get("provenances", [])),
            ))
    return themes


# -----------------------------------------------------------------------------
# Embedding (lazy import: il modulo si carica anche senza sentence-transformers
# installato, utile per test dei soli helper).
# -----------------------------------------------------------------------------

def embed_names(texts: list[str], model_name: str, device: Optional[str]) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "sentence-transformers non installato. "
            "Installa con: pip install sentence-transformers"
        ) from exc
    print(f"       caricamento modello {model_name} (device={device or 'auto'}) ...")
    model = SentenceTransformer(model_name, device=device)
    print(f"       encoding {len(texts)} testi ...")
    emb = model.encode(
        texts,
        normalize_embeddings=True,   # coseno = prodotto scalare
        convert_to_numpy=True,
        show_progress_bar=True,
        batch_size=32,
    )
    print(f"       embedding completato ({emb.shape[0]} x {emb.shape[1]})")
    return emb.astype(np.float32)


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def generate_candidates(
    themes: list[_Theme],
    embed_text: str,
    model_name: str,
    top_k: int,
    cosine_gate: float,
    jaccard_gate: float,
    device: Optional[str],
) -> tuple[dict[tuple[str, str], CandidatePair], np.ndarray]:
    by_id = {t.id: t for t in themes}
    ids = [t.id for t in themes]

    # Testo da embeddare
    if embed_text == "name_desc":
        texts = [f"{t.name}. {t.description or ''}".strip() for t in themes]
    else:
        texts = [t.name for t in themes]
    print(f"       modalità embed: {embed_text} ({len(texts)} testi)")

    emb = embed_names(texts, model_name, device)
    sims = emb @ emb.T  # matrice coseno (embedding normalizzati)
    np.fill_diagonal(sims, -1.0)
    print(f"       matrice similarità: {len(themes)} x {len(themes)}")

    pairs: dict[tuple[str, str], CandidatePair] = {}

    def _ensure(a_id: str, b_id: str) -> CandidatePair:
        key = _pair_key(a_id, b_id)
        if key not in pairs:
            ta, tb = by_id[key[0]], by_id[key[1]]
            pairs[key] = CandidatePair(
                theme_a=ta.id, theme_b=tb.id,
                name_a=ta.name, name_b=tb.name,
                freq_a=ta.freq, freq_b=tb.freq,
                desc_a=ta.description, desc_b=tb.description,
            )
        return pairs[key]

    # Canale denso: top-k vicini sopra gate
    k = min(top_k, len(themes) - 1) if len(themes) > 1 else 0
    print(f"       canale denso: top_k={k}, cosine_gate>={cosine_gate} ...")
    for i in range(len(themes)):
        if k <= 0:
            break
        row = sims[i]
        nbr = np.argpartition(-row, k - 1)[:k]
        for j in nbr:
            c = float(row[j])
            if c < cosine_gate:
                continue
            cp = _ensure(ids[i], ids[int(j)])
            # tieni il coseno massimo se la coppia emerge da entrambe le direzioni
            cp.cosine = c if cp.cosine is None else max(cp.cosine, c)
            if "dense" not in cp.channels:
                cp.channels.append("dense")

    n_dense = sum(1 for cp in pairs.values() if "dense" in cp.channels)
    print(f"       canale denso: {n_dense} coppie")

    # Canale lessicale: Jaccard sui token
    print(f"       canale lessicale: jaccard_gate>={jaccard_gate} ...")
    toks = {t.id: tokenize(t.id, t.name) for t in themes}
    for i in range(len(themes)):
        for j in range(i + 1, len(themes)):
            jac = jaccard(toks[ids[i]], toks[ids[j]])
            if jac >= jaccard_gate:
                cp = _ensure(ids[i], ids[j])
                cp.jaccard = jac
                if "lexical" not in cp.channels:
                    cp.channels.append("lexical")

    n_lexical = sum(1 for cp in pairs.values() if "lexical" in cp.channels)
    print(f"       canale lessicale: {n_lexical} coppie")

    # ordina canali per stabilità
    for cp in pairs.values():
        cp.channels = sorted(cp.channels)

    return pairs, sims


def build_report(
    themes: list[_Theme],
    pairs: dict[tuple[str, str], CandidatePair],
    sims: np.ndarray,
    *,
    source_graph: Path,
    embed_text: str,
    model_name: str,
    top_k: int,
    cosine_gate: float,
    jaccard_gate: float,
    auto_band_cos: float = DEFAULT_AUTO_BAND_COS,
    judge_band_cos: float = DEFAULT_JUDGE_BAND_COS,
    strong_jaccard: float = DEFAULT_STRONG_JACCARD,
) -> CandidateReport:
    cand = sorted(
        pairs.values(),
        key=lambda c: (-(c.cosine or 0.0), -(c.jaccard or 0.0), c.theme_a, c.theme_b),
    )

    dense_only = sum(1 for c in cand if c.channels == ["dense"])
    lexical_only = sum(1 for c in cand if c.channels == ["lexical"])
    both = sum(1 for c in cand if len(c.channels) == 2)

    # distribuzione del coseno del vicino più prossimo (top-1) per ogni tema:
    # racconta "quanto è denso lo spazio" e aiuta a scegliere la soglia.
    if len(themes) > 1:
        top1 = sims.max(axis=1)
        pct = {p: float(np.percentile(top1, q)) for p, q in
               (("p50", 50), ("p75", 75), ("p90", 90), ("p95", 95), ("p99", 99))}
    else:
        pct = {}

    # hub: temi che compaiono in più coppie candidate (possibili over-connessi)
    deg: dict[str, int] = {}
    for c in cand:
        deg[c.theme_a] = deg.get(c.theme_a, 0) + 1
        deg[c.theme_b] = deg.get(c.theme_b, 0) + 1
    name_by_id = {t.id: t.name for t in themes}
    hubs = sorted(deg.items(), key=lambda kv: -kv[1])[:10]
    top_hub = [{"id": tid, "name": name_by_id.get(tid, ""), "n_candidates": n} for tid, n in hubs]

    band_thresholds = {
        "auto_band_cos": auto_band_cos,
        "judge_band_cos": judge_band_cos,
        "strong_jaccard": strong_jaccard,
    }
    band_counts = annotate_bands(
        cand,
        auto_band_cos=auto_band_cos,
        judge_band_cos=judge_band_cos,
        strong_jaccard=strong_jaccard,
    )

    return CandidateReport(
        source_graph=str(source_graph),
        timestamp=datetime.now(timezone.utc),
        model=model_name,
        embed_text=embed_text,
        top_k=top_k,
        cosine_gate=cosine_gate,
        jaccard_gate=jaccard_gate,
        n_themes=len(themes),
        n_candidates=len(cand),
        n_dense_only=dense_only,
        n_lexical_only=lexical_only,
        n_both=both,
        cosine_percentiles=pct,
        top_hub_themes=top_hub,
        band_counts=band_counts,
        band_thresholds=band_thresholds,
        candidates=cand,
    )


def write_outputs(report: CandidateReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "theme_candidates.json"
    tsv_path = out_dir / "theme_candidates.tsv"
    print(f"       scrivo {json_path} ...")
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    # TSV per ispezione rapida
    cols = ["cosine", "jaccard", "channels", "band", "theme_a", "theme_b",
            "name_a", "name_b", "freq_a", "freq_b"]
    lines = ["\t".join(cols)]
    for c in report.candidates:
        lines.append("\t".join([
            f"{c.cosine:.4f}" if c.cosine is not None else "",
            f"{c.jaccard:.4f}" if c.jaccard is not None else "",
            "+".join(c.channels),
            c.band,
            c.theme_a, c.theme_b, c.name_a, c.name_b,
            str(c.freq_a), str(c.freq_b),
        ]))
    print(f"       scrivo {tsv_path} ...")
    tsv_path.write_text("\n".join(lines), encoding="utf-8")


def _print_summary(report: CandidateReport) -> None:
    print(f"stage 5-2a — candidati Theme  (modello {report.model}, embed={report.embed_text})")
    print(f"  Theme totali           : {report.n_themes}")
    print(f"  coppie candidate        : {report.n_candidates}")
    print(f"    solo denso           : {report.n_dense_only}")
    print(f"    solo lessicale       : {report.n_lexical_only}")
    print(f"    entrambi             : {report.n_both}")
    if report.cosine_percentiles:
        pct = report.cosine_percentiles
        print("  coseno vicino top-1 (distribuzione):")
        print("    p50={p50:.3f}  p75={p75:.3f}  p90={p90:.3f}  p95={p95:.3f}  p99={p99:.3f}".format(**pct))
    print(f"  soglie usate           : cosine_gate={report.cosine_gate}  jaccard_gate={report.jaccard_gate}  top_k={report.top_k}")
    print("  temi più 'hub' (n. candidati):")
    for h in report.top_hub_themes:
        print(f"    {h['n_candidates']:3d}  {h['id']}")
    print("  prime 15 coppie per coseno:")
    for c in report.candidates[:15]:
        cs = f"{c.cosine:.3f}" if c.cosine is not None else "  -  "
        jc = f"{c.jaccard:.2f}" if c.jaccard is not None else " - "
        print(f"    cos={cs} jac={jc} [{'+'.join(c.channels)}]  {c.theme_a}  ~  {c.theme_b}")

    n = report.n_candidates or 1
    bc = report.band_counts
    bt = report.band_thresholds
    print("  bande diagnostiche (non filtrano l'output):")
    print(
        f"    soglie: auto>={bt.get('auto_band_cos')}  "
        f"judge>={bt.get('judge_band_cos')}  strong_jaccard>={bt.get('strong_jaccard')}"
    )
    parts = []
    for band in ("auto", "judge", "drop"):
        cnt = bc.get(band, 0)
        pct_band = 100.0 * cnt / n
        parts.append(f"{band}: {cnt} ({pct_band:.1f}%)")
    print("    " + "  ".join(parts))

    print("  istogramma coseno (bin 0.05, solo coppie con coseno):")
    hist = _cosine_histogram_bins(report.candidates)
    max_cnt = max((c for _, c in hist), default=0)
    bar_width = 40
    for label, cnt in hist:
        bar_len = int(round(cnt / max_cnt * bar_width)) if max_cnt else 0
        bar = "#" * bar_len
        print(f"    [{label}]  {cnt:4d}  {bar}")

    hub_ids = {h["id"] for h in report.top_hub_themes}
    judge_pairs = [c for c in report.candidates if c.band == "judge"]
    hub_judge_pairs = [
        c for c in judge_pairs
        if c.theme_a in hub_ids or c.theme_b in hub_ids
    ]
    hub_judge_touch: dict[str, int] = {hid: 0 for hid in hub_ids}
    for c in judge_pairs:
        if c.theme_a in hub_judge_touch:
            hub_judge_touch[c.theme_a] += 1
        if c.theme_b in hub_judge_touch:
            hub_judge_touch[c.theme_b] += 1
    print(f"  judge-band con tema hub (top-10): {len(hub_judge_pairs)} / {len(judge_pairs)} coppie")
    for h in report.top_hub_themes:
        n_j = hub_judge_touch.get(h["id"], 0)
        print(f"    {n_j:3d} judge  {h['id']}")

    judge_sample = sorted(
        judge_pairs,
        key=lambda c: (-(c.cosine if c.cosine is not None else -1.0), c.theme_a, c.theme_b),
    )[:10]
    print("  campione judge-band (10 coppie, coseno decrescente):")
    for c in judge_sample:
        cs = f"{c.cosine:.3f}" if c.cosine is not None else "  -  "
        jc = f"{c.jaccard:.2f}" if c.jaccard is not None else " - "
        print(f"    cos={cs} jac={jc}  {c.name_a}  ~  {c.name_b}")
        print(f"      A: {_trunc_text(c.desc_a)}")
        print(f"      B: {_trunc_text(c.desc_b)}")


def run(
    input_path: Path,
    out_dir: Path,
    embed_text: str = "name",
    model_name: str = DEFAULT_MODEL,
    top_k: int = DEFAULT_TOP_K,
    cosine_gate: float = DEFAULT_COSINE_GATE,
    jaccard_gate: float = DEFAULT_JACCARD_GATE,
    auto_band_cos: float = DEFAULT_AUTO_BAND_COS,
    judge_band_cos: float = DEFAULT_JUDGE_BAND_COS,
    strong_jaccard: float = DEFAULT_STRONG_JACCARD,
    device: Optional[str] = None,
    dry_run: bool = False,
) -> CandidateReport:
    print("[5-2a] avvio")
    print(f"       input={input_path}")
    print(f"       modello={model_name}  embed={embed_text}  device={device or 'auto'}")
    print(f"       top_k={top_k}  cosine_gate={cosine_gate}  jaccard_gate={jaccard_gate}")
    print(
        f"       bande: auto>={auto_band_cos}  judge>={judge_band_cos}  "
        f"strong_jaccard>={strong_jaccard}"
    )
    if dry_run:
        print("       dry-run: nessun file scritto")
    else:
        print(f"       output={out_dir}")

    print(f"[5-2a] caricamento Theme da {input_path} ...")
    themes = load_themes(input_path)
    if not themes:
        raise SystemExit(f"nessun nodo Theme trovato in {input_path}")
    print(f"       {len(themes)} Theme trovati")

    print("[5-2a] generazione candidati ...")
    pairs, sims = generate_candidates(
        themes, embed_text, model_name, top_k, cosine_gate, jaccard_gate, device
    )
    print(f"       totale coppie uniche: {len(pairs)}")

    print("[5-2a] costruzione report ...")
    report = build_report(
        themes, pairs, sims,
        source_graph=input_path, embed_text=embed_text, model_name=model_name,
        top_k=top_k, cosine_gate=cosine_gate, jaccard_gate=jaccard_gate,
        auto_band_cos=auto_band_cos, judge_band_cos=judge_band_cos,
        strong_jaccard=strong_jaccard,
    )
    if not dry_run:
        print("[5-2a] scrittura output ...")
        write_outputs(report, out_dir)
    print("[5-2a] completato")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Stadio 5-2a: candidati per il consolidamento Theme.")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="grafo arricchito (output 5-1)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="cartella di output")
    ap.add_argument("--embed", choices=["name", "name_desc"], default="name", help="testo da embeddare")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="modello di embedding (sentence-transformers)")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="vicini densi per tema")
    ap.add_argument("--cosine-gate", type=float, default=DEFAULT_COSINE_GATE, help="coseno minimo (canale denso)")
    ap.add_argument("--jaccard-gate", type=float, default=DEFAULT_JACCARD_GATE, help="Jaccard minimo (canale lessicale, default 0.30)")
    ap.add_argument("--auto-band-cos", type=float, default=DEFAULT_AUTO_BAND_COS, help="soglia coseno banda auto (default 0.92)")
    ap.add_argument("--judge-band-cos", type=float, default=DEFAULT_JUDGE_BAND_COS, help="soglia coseno minima banda judge (default 0.80)")
    ap.add_argument("--strong-jaccard", type=float, default=DEFAULT_STRONG_JACCARD, help="Jaccard forte → banda judge (default 0.60)")
    ap.add_argument("--device", default="cuda", help="es. 'cuda', 'cpu' (default: cuda)")
    ap.add_argument("--dry-run", action="store_true", help="non scrive file, stampa solo il riepilogo")
    args = ap.parse_args()

    report = run(
        args.input, args.out,
        embed_text=args.embed, model_name=args.model, top_k=args.top_k,
        cosine_gate=args.cosine_gate, jaccard_gate=args.jaccard_gate,
        auto_band_cos=args.auto_band_cos, judge_band_cos=args.judge_band_cos,
        strong_jaccard=args.strong_jaccard,
        device=args.device, dry_run=args.dry_run,
    )
    _print_summary(report)


if __name__ == "__main__":
    main()