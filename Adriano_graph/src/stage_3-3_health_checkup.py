# src/stage_3-3_health_checkup.py
"""
STADIO 3 — FASE 3: HEALTH CHECKUP (checkpoint post-estrazione)

Convalida che l'estrazione (extracted_graph.json) sia pronta per lo stadio 4 resolve.
Usa tools/extraction_analysis.py per le metriche; aggiunge checks, verdetto, dashboard.

Uso:
  python src/stage_3-3_health_checkup.py
  python src/stage_3-3_health_checkup.py -i data/stage_3/full_runs/<run>/extracted_graph.json
  python src/stage_3-3_health_checkup.py --plotly   # anche report Plotly legacy

Input default: extracted_graph.json più recente in data/stage_3/full_runs/
Output: data/stage_3/3_health_checkup/
  dashboard.html      aprire per primo
  metrics.json      metriche complete (extraction_analysis)
  checks.json       controlli con guida
  review_queue.json typing_warnings, chunk vuoti, event densi
  health_log.json   verdetto e metadata
  report_plotly.html  solo con --plotly

Verdetto: pass | pass_with_warnings | fail
Vedi ADR-022, stage_4-5_health_checkup.py (stesso pattern).
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from extraction_analysis import run as ea_run  # noqa: E402

OUT_DIR = PROJECT_ROOT / "data" / "stage_3" / "3_health_checkup"
FULL_RUNS_DIR = PROJECT_ROOT / "data" / "stage_3" / "full_runs"

STAGE_VERSION = "0.1.0"
DEFAULT_SUBJECT_ID = "adriano"

CheckStatus = Literal["pass", "warn", "fail", "info", "skip"]
Verdict = Literal["pass", "pass_with_warnings", "fail"]

# Catalogo controlli (allineato a summary.verdict_table in extraction_analysis)
CHECK_CATALOG: dict[str, dict[str, Any]] = {
    "graph_loaded": {
        "title": "Grafo estratto caricabile",
        "category": "struttura",
        "blocks_stage": True,
        "why": "extracted_graph.json deve parsare e contenere extractions valide.",
        "what_to_check": "Nessun errore di load; chunk processati > 0.",
        "solutions": ["Rilanciare stage_3-2_extract sul batch fallito.", "Ispezionare extraction_log.json."],
    },
    "empty_chunks": {
        "title": "Chunk senza estrazione",
        "category": "struttura",
        "blocks_stage": True,
        "why": "Chunk a zero nodi/archi indicano fallimento silenzioso o skip.",
        "what_to_check": "empty_chunks_count = 0.",
        "solutions": ["Rieseguire estrazione sui chunk_id elencati.", "Verificare errori nel log batch."],
    },
    "caused_follows_ratio": {
        "title": "Rapporto CAUSED / FOLLOWS",
        "category": "narrativa",
        "blocks_stage": False,
        "why": "CAUSED alto = grafo causale, non solo cronaca.",
        "what_to_check": "ratio >= 0.6 pass; < 0.4 stop.",
        "solutions": ["Regola CAUSED vs FOLLOWS in stage_3-1_prompt.", "Few-shot con CAUSED espliciti."],
    },
    "pct_event_with_during": {
        "title": "Event con ancoraggio temporale",
        "category": "tempo",
        "blocks_stage": False,
        "why": "DURING su Era/Phase ancora la biografia nel tempo.",
        "what_to_check": "pct_with_during_any >= 70%.",
        "solutions": ["Prompt 0.4.0: Era obbligatoria.", "Verificare few-shot con DURING."],
    },
    "pct_involves_valid_role": {
        "title": "INVOLVES con role valido",
        "category": "schema",
        "blocks_stage": False,
        "why": "Schema 0.2.0 richiede role su INVOLVES.",
        "what_to_check": "pct >= 98%.",
        "solutions": ["Aggiornare few-shot con role.", "Bump prompt se modello omette role."],
    },
    "pct_protagonist_on_involves": {
        "title": "Bilanciamento role protagonist",
        "category": "schema",
        "blocks_stage": False,
        "why": "Troppi protagonist = effetto stella; troppo pochi = soggetto sotto-rappresentato.",
        "what_to_check": "40-65% su tutti gli INVOLVES.",
        "solutions": ["Calibrare regola protagonist in prompt.", "Review hub adriano."],
    },
    "themes_per_chunk": {
        "title": "Densità Theme per chunk",
        "category": "temi",
        "blocks_stage": False,
        "why": "Troppi Theme/chunk gonfia dedup e enrich.",
        "what_to_check": "<= 1.0 Theme/chunk.",
        "solutions": ["Regola canonicità name Theme in prompt.", "Theme incarnato senza moltiplicare name."],
    },
    "top_theme_embodies_in": {
        "title": "Theme hub (EMBODIES in)",
        "category": "temi",
        "blocks_stage": False,
        "why": "Serve almeno un Theme centrale con molti EMBODIES.",
        "what_to_check": "top_theme_embodies_in >= 10.",
        "solutions": ["Verificare EMBODIES Event→Theme nel prompt."],
    },
    "typing_warnings_per_100_chunks": {
        "title": "Collisioni tipo (stesso id, type diverso)",
        "category": "dedup preview",
        "blocks_stage": False,
        "why": "Anticipa lavoro stadio 4 split/merge; molte collisioni = prompt ambiguo.",
        "what_to_check": "<= 3 per 100 chunk.",
        "solutions": ["Regole disambiguazione Event/Theme/Place in prompt 0.4.", "Review typing_warnings in review_queue."],
    },
    "pct_phase_occurs_in_era": {
        "title": "Phase agganciate a Era",
        "category": "tempo",
        "blocks_stage": False,
        "why": "OCCURS_IN Phase→Era collega fasi emergenti alla griglia temporale.",
        "what_to_check": ">= 80% Phase con OCCURS_IN verso Era.",
        "solutions": ["Prompt Era + OCCURS_IN.", "Verificare schema 0.2.0."],
    },
    "low_echoes_count": {
        "title": "ECHOES intra-chunk bassi",
        "category": "atteso",
        "blocks_stage": False,
        "why": "ECHOES cross-chunk non sono compito stadio 3.",
        "what_to_check": "Conteggio basso è normale; enrich in stadio 5.",
        "solutions": ["Non correggere in estrazione.", "Pianificare stadio 5 enrich."],
    },
}


def resolve_default_input() -> Path:
    candidates = list(FULL_RUNS_DIR.glob("*/extracted_graph.json"))
    if not candidates:
        raise FileNotFoundError(
            f"Nessun extracted_graph.json in {FULL_RUNS_DIR}. "
            "Passa -i esplicitamente."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _map_ea_status(ea_status: str) -> CheckStatus:
    if ea_status == "pass":
        return "pass"
    if ea_status == "stop":
        return "fail"
    if ea_status == "fail":
        return "warn"
    return "skip"


def build_checks(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    meta = metrics.get("run_metadata", {})
    summary = metrics.get("summary", {})
    counts = metrics.get("counts", {})
    prov = metrics.get("provenance", {})
    arcs = metrics.get("narrative_arcs", {})
    table = summary.get("verdict_table", {})

    chunks = meta.get("chunks_processed") or summary.get("chunks_in_run") or 0
    results.append({
        "check_id": "graph_loaded",
        **{k: CHECK_CATALOG["graph_loaded"][k] for k in ("title", "category", "blocks_stage", "why", "what_to_check", "solutions")},
        "status": "pass" if chunks > 0 else "fail",
        "count": chunks,
        "value": chunks,
        "target": "> 0",
        "message": f"{chunks} chunk processati.",
        "samples": [],
    })

    empty = prov.get("empty_chunks_count", 0)
    results.append({
        "check_id": "empty_chunks",
        **{k: CHECK_CATALOG["empty_chunks"][k] for k in ("title", "category", "blocks_stage", "why", "what_to_check", "solutions")},
        "status": "fail" if empty > 0 else "pass",
        "count": empty,
        "value": empty,
        "target": "0",
        "message": f"{empty} chunk senza estrazione.",
        "samples": [{"chunk_id": c} for c in prov.get("empty_chunks", [])[:8]],
    })

    for check_id, row in table.items():
        if check_id not in CHECK_CATALOG:
            continue
        cat = CHECK_CATALOG[check_id]
        ea_st = row.get("status", "n/a")
        if ea_st == "n/a":
            st: CheckStatus = "skip"
            msg = "Non applicabile a questo run."
        else:
            st = _map_ea_status(ea_st)
            val = row.get("value")
            tgt = row.get("target", "")
            thr = row.get("stop_threshold", "")
            msg = f"valore {val}; target {tgt}; stop {thr} → {ea_st}"
        results.append({
            "check_id": check_id,
            "title": cat["title"],
            "category": cat["category"],
            "blocks_stage": cat["blocks_stage"] and st == "fail",
            "status": st,
            "count": row.get("distinct_id_count") or row.get("warning_entry_count") or 0,
            "value": row.get("value"),
            "target": row.get("target", ""),
            "message": msg,
            "why": cat["why"],
            "what_to_check": cat["what_to_check"],
            "solutions": cat["solutions"],
            "samples": [],
        })

    echoes = arcs.get("counts", {}).get("ECHOES") or counts.get("by_edge_type", {}).get("ECHOES", 0)
    cat = CHECK_CATALOG["low_echoes_count"]
    results.append({
        "check_id": "low_echoes_count",
        "title": cat["title"],
        "category": cat["category"],
        "blocks_stage": False,
        "status": "info",
        "count": echoes,
        "value": echoes,
        "target": "basso atteso",
        "message": f"{echoes} ECHOES — cross-chunk in stadio 5 enrich.",
        "why": cat["why"],
        "what_to_check": cat["what_to_check"],
        "solutions": cat["solutions"],
        "samples": [],
    })

    order = {"fail": 0, "warn": 1, "pass": 2, "info": 3, "skip": 4}
    results.sort(key=lambda x: (order.get(x["status"], 9), x["check_id"]))
    return results


def build_review_queue(metrics: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    for w in metrics.get("typing_warnings", []):
        items.append({
            "priority": 1,
            "kind": "typing_collision",
            "entity_id": w.get("id"),
            "reason": f"type discordante: {w.get('types')}",
            "chunk_ids": w.get("chunks", []),
            "sample": w,
        })

    for cid in metrics.get("provenance", {}).get("empty_chunks", []):
        items.append({
            "priority": 1,
            "kind": "chunk",
            "entity_id": cid,
            "reason": "empty_extraction",
            "chunk_ids": [cid],
        })

    eq = metrics.get("event_quality", {})
    for ev in (eq.get("high_degree") or [])[:8]:
        items.append({
            "priority": 2,
            "kind": "event",
            "entity_id": ev.get("id"),
            "reason": f"high_degree (>{eq.get('high_degree_threshold')})",
            "chunk_ids": ev.get("chunks", []),
            "sample": ev,
        })

    items.sort(key=lambda x: (x["priority"], str(x.get("entity_id", ""))))
    return {"total": len(items), "items": items}


def compute_verdict(checks: list[dict[str, Any]], ea_verdict: str) -> dict[str, Any]:
    fails = [c for c in checks if c["status"] == "fail" and c.get("blocks_stage")]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails or ea_verdict == "stop":
        verdict: Verdict = "fail"
        headline = "Stadio 3 NON convalidato — correggere estrazione/prompt prima del resolve."
    elif warns or ea_verdict == "mixed":
        verdict = "pass_with_warnings"
        headline = "Stadio 3 convalidato con avvisi — review consigliata, si può procedere al resolve."
    else:
        verdict = "pass"
        headline = "Stadio 3 convalidato — pronto per stadio 4 resolve."

    return {
        "verdict": verdict,
        "headline": headline,
        "extraction_analysis_verdict": ea_verdict,
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


def render_dashboard(
    metrics: dict[str, Any],
    checks: list[dict[str, Any]],
    verdict: dict[str, Any],
    health_log: dict[str, Any],
    review_queue: dict[str, Any],
) -> str:
    meta = metrics.get("run_metadata", {})
    counts = metrics.get("counts", {})
    summary = metrics.get("summary", {})
    arcs = metrics.get("narrative_arcs", {})

    cards = [
        ("Chunk", meta.get("chunks_processed")),
        ("Nodi", counts.get("nodes_total")),
        ("Archi", counts.get("edges_total")),
        ("Prompt", meta.get("prompt_version")),
        ("CAUSED/F", round(arcs.get("caused_follows_ratio", 0), 2) if arcs.get("caused_follows_ratio") else "n/a"),
        ("ECHOES", arcs.get("counts", {}).get("ECHOES")),
        ("Review", review_queue["total"]),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="card-n">{html.escape(str(v))}</div>'
        f'<div class="card-l">{html.escape(k)}</div></div>'
        for k, v in cards
    )

    vcolors = {
        "pass": ("#0d7a4a", "#e6f4ed"),
        "pass_with_warnings": ("#9a6700", "#fff8e6"),
        "fail": ("#b42318", "#fdecea"),
    }
    fg, bg = vcolors.get(verdict["verdict"], ("#333", "#eee"))
    banner = f"""
    <div class="verdict" style="background:{bg};border-left:4px solid {fg}">
      <div class="verdict-label" style="color:{fg}">{verdict['verdict'].replace('_',' ').upper()}</div>
      <div>{html.escape(verdict['headline'])}</div>
      <div class="muted">extraction_analysis: {html.escape(verdict.get('extraction_analysis_verdict',''))}
        · pass {summary.get('verdict_pass_count')} · stop {summary.get('verdict_stop_count')}</div>
    </div>"""

    rows = []
    for c in checks:
        sol = "<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in c["solutions"]) + "</ul>"
        samples = ""
        if c.get("samples"):
            samples = "<ul class='samples'>" + "".join(
                f"<li><code>{html.escape(str(s.get('chunk_id', s.get('id', s))))}</code></li>"
                for s in c["samples"][:5]
            ) + "</ul>"
        rows.append(f"""
        <tr class="status-{c['status']}">
          <td>{_status_badge(c['status'])}</td>
          <td><b>{html.escape(c['title'])}</b><br><span class="muted">{html.escape(c['check_id'])}</span></td>
          <td>{html.escape(c['message'])}</td>
          <td>{html.escape(c['why'])}</td>
          <td><p>{html.escape(c['what_to_check'])}</p>{sol}{samples}</td>
        </tr>""")

    guide = "".join(
        f"<li><b>{html.escape(v['title'])}</b>: {html.escape(v['why'][:100])}…</li>"
        for v in CHECK_CATALOG.values()
    )

    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<title>Health checkup — Stadio 3 Extract</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:20px;background:#fafafa;color:#1a1a1a}}
h1{{margin:0 0 6px}} .muted{{color:#667085;font-size:.9rem}}
.verdict{{padding:14px 18px;border-radius:8px;margin:16px 0}}
.cards{{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}}
.card{{background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:10px 14px;min-width:90px}}
.card-n{{font-size:1.3rem;font-weight:700}} .card-l{{font-size:.78rem;color:#667085}}
.badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.72rem;font-weight:700}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:.9rem}}
th,td{{border:1px solid #e4e7ec;padding:8px 10px;vertical-align:top}}
th{{background:#f2f4f7;text-align:left}}
.guide{{background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:14px}}
code{{background:#f2f4f7;padding:1px 4px;border-radius:3px}}
.samples{{font-size:.82rem}}
</style></head><body>
<h1>Health checkup — Stadio 3 Extract</h1>
<p class="muted">stage_3-3_health_checkup v{STAGE_VERSION} · {html.escape(health_log.get('timestamp',''))}
 · input <code>{html.escape(health_log.get('input_graph',''))}</code></p>
{banner}
<h2>Metriche</h2><div class="cards">{cards_html}</div>
<h2>Guida controlli</h2><div class="guide"><ul>{guide}</ul>
<p class="muted">Metriche complete in metrics.json (extraction_analysis).</p></div>
<h2>Esito controlli</h2>
<table><thead><tr><th>Status</th><th>Controllo</th><th>Messaggio</th><th>Perché</th><th>Verifica / Soluzioni</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<h2>Review umana ({review_queue['total']})</h2>
<p class="muted">review_queue.json — typing_warnings, chunk vuoti, Event ad alto grado.</p>
<h2>Artefatti</h2>
<ul>
<li>dashboard.html</li><li>metrics.json</li><li>checks.json</li>
<li>review_queue.json</li><li>health_log.json</li>
</ul>
</body></html>"""


def run(
    input_path: Path | None = None,
    out_dir: Path = OUT_DIR,
    log_path: Path | None = None,
    subject_id: str = DEFAULT_SUBJECT_ID,
    write_plotly: bool = False,
    print_summary: bool = False,
) -> dict[str, Any]:
    graph_path = input_path or resolve_default_input()
    log_path = log_path or (graph_path.parent / "extraction_log.json")

    out_dir.mkdir(parents=True, exist_ok=True)

    ea_run(
        input_path=graph_path,
        output_dir=out_dir,
        subject_id=subject_id,
        event_high_degree=10,
        low_confidence_threshold=0.5,
        print_to_stdout=print_summary,
        log_path=log_path if log_path.is_file() else None,
        write_html=write_plotly,
    )

    if write_plotly:
        plotly_src = out_dir / "report.html"
        if plotly_src.is_file():
            plotly_src.rename(out_dir / "report_plotly.html")

    metrics = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    checks = build_checks(metrics)
    ea_verdict = metrics.get("summary", {}).get("verdict", "mixed")
    verdict = compute_verdict(checks, ea_verdict)
    review_queue = build_review_queue(metrics)

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(p)

    health_log = {
        "stage": "stage_3-3_health_checkup",
        "stage_version": STAGE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_graph": _rel(graph_path),
        "input_log": _rel(log_path) if log_path.is_file() else None,
        "output_dir": _rel(out_dir),
        "prompt_version": metrics.get("run_metadata", {}).get("prompt_version"),
        "schema_version": metrics.get("run_metadata", {}).get("schema_version"),
        "chunks_processed": metrics.get("run_metadata", {}).get("chunks_processed"),
        "verdict": verdict,
        "summary": {
            "checks_total": len(checks),
            "review_queue_total": review_queue["total"],
        },
    }

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
        render_dashboard(metrics, checks, verdict, health_log, review_queue),
        encoding="utf-8",
    )

    print(f"input:  {graph_path}")
    print(f"output: {out_dir}/")
    print(f"  verdetto: {verdict['verdict'].upper()} — {verdict['headline']}")
    print(f"  dashboard: {out_dir / 'dashboard.html'}")
    print(f"  review queue: {review_queue['total']} item")
    return health_log


def main() -> None:
    ap = argparse.ArgumentParser(description="Health checkup stadio 3 (extracted_graph)")
    ap.add_argument("-i", "--input", type=Path, default=None, help="extracted_graph.json")
    ap.add_argument("-l", "--log", type=Path, default=None, help="extraction_log.json")
    ap.add_argument("-o", "--output-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--subject-id", default=DEFAULT_SUBJECT_ID)
    ap.add_argument("--plotly", action="store_true", help="Genera anche report_plotly.html")
    ap.add_argument("--print", dest="print_summary", action="store_true")
    args = ap.parse_args()
    run(
        input_path=args.input,
        out_dir=args.output_dir,
        log_path=args.log,
        subject_id=args.subject_id,
        write_plotly=args.plotly,
        print_summary=args.print_summary,
    )


if __name__ == "__main__":
    main()
