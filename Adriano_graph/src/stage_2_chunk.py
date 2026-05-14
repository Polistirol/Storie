"""
Stadio 2: chunking per estrazione (un chunk per paragrafo, accorpamento brevi).

Esecuzione (cwd = Adriano_graph/):
    python src/stage_2_chunk.py

Path e parametri sono costanti in cima; nessun argparse.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken

# -----------------------------------------------------------------------------
# Costanti modificabili
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CLEANED_TEXT_REL = "data/stage_1/cleaned_text.txt"
RAW_TEXT_REL = "data/stage_0/raw_text.txt"
STRUCTURE_REL = "data/stage_0/structure.json"
OUTPUT_DIR_REL = "data/stage_2"
CHUNKS_JSON_REL = "data/stage_2/chunks.json"
CHUNKING_LOG_REL = "data/stage_2/chunking_log.json"

STAGE_VERSION = "0.1.0"
MIN_TOKENS = 80
TOKEN_ENCODER = "cl100k_base"


# -----------------------------------------------------------------------------
# Tipi
# -----------------------------------------------------------------------------


@dataclass
class Paragraph:
    """Paragrafo con offset nel file e parte assegnata (dopo filtro titoli: sempre body)."""

    index: int  # indice nella lista lavorata (post-filtro titoli)
    text: str
    char_start: int
    char_end: int
    part_number: int
    part_title: str


@dataclass
class ChunkDraft:
    """Chunk prima dell'assegnazione di chunk_id (numerazione globale a fine build)."""

    part_number: int
    part_title: str
    position_in_part: int  # riempito dopo
    char_start: int
    char_end: int
    token_count: int
    paragraph_indices: list[int]
    merged: bool
    text: str
    warnings: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Utilità
# -----------------------------------------------------------------------------


def abort(reason: str) -> None:
    print(reason, file=sys.stderr)
    raise SystemExit(1)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_dumps_stable(obj: object) -> str:
    # Ordine inserimento preservato (Python 3.7+); no sort_keys per leggibilità dei chunk.
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# -----------------------------------------------------------------------------
# Caricamento e segmentazione
# -----------------------------------------------------------------------------


def load_paragraphs_with_offsets(text: str) -> list[dict[str, Any]]:
    """
    Splitta su '\\n\\n' e calcola [char_start, char_end) per ogni segmento.

    Separatore doppio a capo è la stessa convenzione dello stadio 1; gli offset
    restano allineati a structure.json calcolato su raw/cleaned identici.
    """
    paragraphs: list[dict[str, Any]] = []
    pos = 0
    segments = text.split("\n\n")
    for seg in segments:
        char_start = pos
        char_end = pos + len(seg)
        paragraphs.append({"text": seg, "char_start": char_start, "char_end": char_end})
        pos = char_end
        if pos < len(text):
            pos += 2  # salta il separatore \n\n tra questo e il prossimo
    return paragraphs


def assign_parts(
    paragraphs: list[dict[str, Any]], parts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Associa ogni paragrafo a una parte: titoli via title_char/testo, corpo via body_*.

    I titoli stanno fuori dagli intervalli body_* quindi hanno ramo dedicato.
    """
    out: list[dict[str, Any]] = []
    for p in paragraphs:
        title_hits = [pt for pt in parts if paragraph_matches_part_title(p, pt)]
        if len(title_hits) > 1:
            abort(
                f"Paragrafo che combacia con più titoli: "
                f"[{p['char_start']},{p['char_end']}): {p['text'][:120]!r}…"
            )
        if len(title_hits) == 1:
            pt = title_hits[0]
            q = dict(p)
            q["part_number"] = pt["number"]
            q["part_title"] = pt["title"]
            q["is_title_paragraph"] = True
            out.append(q)
            continue

        cs = p["char_start"]
        hit: dict[str, Any] | None = None
        for pt in parts:
            lo, hi = pt["body_char_start"], pt["body_char_end"]
            if lo <= cs < hi:
                hit = pt
                break
        if hit is None:
            abort(
                f"Paragrafo fuori da ogni parte (problema a monte): "
                f"char_start={cs}, estratto={p['text'][:120]!r}…"
            )
        q = dict(p)
        q["part_number"] = hit["number"]
        q["part_title"] = hit["title"]
        q["is_title_paragraph"] = False
        out.append(q)
    return out


def paragraph_matches_part_title(p: dict[str, Any], part: dict[str, Any]) -> bool:
    """Titoli di parte: span in structure oppure testo normalizzato (paragrafo isolato)."""
    span_match = (
        p["char_start"] == part["title_char_start"] and p["char_end"] == part["title_char_end"]
    )
    text_match = p["text"].strip() == part["title"]
    return span_match or text_match


def filter_part_titles(
    paragraphs: list[dict[str, Any]], parts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Rimuove i sei titoli di parte. Se il conteggio ≠ 6, abort con elenco match.

    Restituisce (paragrafi_contenuto, elenco titoli filtrati per diagnostica).
    """
    if len(parts) != 6:
        abort(f"structure.json: attese 6 parti, trovate {len(parts)}.")

    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for p in paragraphs:
        if p.get("is_title_paragraph"):
            pt = next(
                (x for x in parts if x["number"] == p["part_number"]),
                None,
            )
            if pt is None:
                abort(f"Titolo paragrafo senza parte corrispondente: {p}.")
            matched.append((p, pt))

    if len(matched) != 6:
        lines = [
            (
                f"  - #{i+1} chars [{p['char_start']},{p['char_end']}): "
                f"{p['text']!r} (parte {pt['number']})"
            )
            for i, (p, pt) in enumerate(matched)
        ]
        abort(
            "Attesi esattamente 6 paragrafi-titolo da filtrare; "
            f"trovati={len(matched)}.\nElenco match:\n"
            + ("\n".join(lines) if lines else "  (nessun match)")
        )

    matched_set = {id(p) for p, _ in matched}
    kept = [p for p in paragraphs if id(p) not in matched_set]

    excluded_meta = [
        {
            "part_number": pt["number"],
            "title": pt["title"],
            "char_start": p["char_start"],
            "char_end": p["char_end"],
            "text_preview": p["text"][:80],
        }
        for p, pt in matched
    ]
    return kept, excluded_meta


def count_tokens(text: str, enc: tiktoken.Encoding) -> int:
    """Proxy dimensione chunk per policy ADR-006 (no split oltre il paragrafo)."""
    return len(enc.encode(text, disallowed_special=()))


# -----------------------------------------------------------------------------
# Merge
# -----------------------------------------------------------------------------


def paragraph_counts_in_part(paragraphs: list[Paragraph]) -> dict[int, int]:
    c: dict[int, int] = {}
    for p in paragraphs:
        c[p.part_number] = c.get(p.part_number, 0) + 1
    return c


def merge_small_paragraphs(
    paragraphs: list[Paragraph],
    enc: tiktoken.Encoding,
) -> tuple[list[ChunkDraft], list[dict[str, Any]]]:
    """
    Applica soglia MIN_TOKENS: merge in avanti se c'è il successivo stessa parte,
    altrietti merge indietro sull'ultimo chunk (salvo unico paragrafo della parte).

    Un solo accoppiamento per volta: se dopo il merge i token restano sotto soglia,
    si segnala ma non si ri-merge.
    """
    log_warnings: list[dict[str, Any]] = []
    chunks: list[ChunkDraft] = []
    n = len(paragraphs)
    counts = paragraph_counts_in_part(paragraphs)

    i = 0
    while i < n:
        p = paragraphs[i]
        tok = count_tokens(p.text, enc)

        if tok >= MIN_TOKENS:
            chunks.append(
                ChunkDraft(
                    part_number=p.part_number,
                    part_title=p.part_title,
                    position_in_part=0,
                    char_start=p.char_start,
                    char_end=p.char_end,
                    token_count=tok,
                    paragraph_indices=[p.index],
                    merged=False,
                    text=p.text,
                )
            )
            i += 1
            continue

        has_next_same = i + 1 < n and paragraphs[i + 1].part_number == p.part_number
        if has_next_same:
            p2 = paragraphs[i + 1]
            merged_text = p.text + "\n\n" + p2.text
            merged_tok = count_tokens(merged_text, enc)
            wtypes: list[str] = []
            if merged_tok < MIN_TOKENS:
                wtypes.append("still_below_min_after_merge")
                log_warnings.append(
                    {
                        "type": "still_below_min_after_merge",
                        "chunk_index_placeholder": len(chunks),
                        "paragraph_indices": [p.index, p2.index],
                        "tokens": merged_tok,
                        "note": (
                            f"Dopo merge in avanti, token={merged_tok} < {MIN_TOKENS} "
                            "(nessun ulteriore accorpamento, policy stadio 2)."
                        ),
                    }
                )
            chunks.append(
                ChunkDraft(
                    part_number=p.part_number,
                    part_title=p.part_title,
                    position_in_part=0,
                    char_start=p.char_start,
                    char_end=p2.char_end,
                    token_count=merged_tok,
                    paragraph_indices=[p.index, p2.index],
                    merged=True,
                    text=merged_text,
                    warnings=wtypes,
                )
            )
            i += 2
            continue

        if counts[p.part_number] == 1:
            log_warnings.append(
                {
                    "type": "lonely_small_paragraph",
                    "chunk_index_placeholder": len(chunks),
                    "paragraph_index": p.index,
                    "tokens": tok,
                    "note": (
                        f"Unico paragrafo della parte {p.part_number}, token={tok} < {MIN_TOKENS}: "
                        "chunk sottile emesso così com'è."
                    ),
                }
            )
            chunks.append(
                ChunkDraft(
                    part_number=p.part_number,
                    part_title=p.part_title,
                    position_in_part=0,
                    char_start=p.char_start,
                    char_end=p.char_end,
                    token_count=tok,
                    paragraph_indices=[p.index],
                    merged=False,
                    text=p.text,
                    warnings=["lonely_small_paragraph"],
                )
            )
            i += 1
            continue

        if not chunks:
            abort(
                f"Merge all'indietro impossibile (nessun chunk precedente) per paragrafo "
                f"index={p.index} parte={p.part_number}."
            )
        prev_chunk = chunks.pop()
        if prev_chunk.part_number != p.part_number:
            abort(
                f"Paragrafo piccolo in coda parte senza chunk precedente nella stessa parte: "
                f"p.index={p.index} part={p.part_number}, ultimo_chunk_part={prev_chunk.part_number}"
            )
        merged_text = prev_chunk.text + "\n\n" + p.text
        merged_tok = count_tokens(merged_text, enc)
        wtypes = list(prev_chunk.warnings)
        if merged_tok < MIN_TOKENS:
            wtypes.append("still_below_min_after_merge")
            log_warnings.append(
                {
                    "type": "still_below_min_after_merge",
                    "chunk_index_placeholder": len(chunks),
                    "paragraph_indices": prev_chunk.paragraph_indices + [p.index],
                    "tokens": merged_tok,
                    "note": (
                        f"Dopo merge indietro, token={merged_tok} < {MIN_TOKENS}; "
                        "nessun ulteriore accorpamento."
                    ),
                }
            )
        extended = ChunkDraft(
            part_number=prev_chunk.part_number,
            part_title=prev_chunk.part_title,
            position_in_part=0,
            char_start=prev_chunk.char_start,
            char_end=p.char_end,
            token_count=merged_tok,
            paragraph_indices=prev_chunk.paragraph_indices + [p.index],
            merged=True,
            text=merged_text,
            warnings=wtypes,
        )
        chunks.append(extended)
        i += 1

    return chunks, log_warnings


def build_chunks(
    paragraphs: list[dict[str, Any]],
    enc: tiktoken.Encoding,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """
    Converte paragrafi filtrati in Paragraph numerati, merge, poi assegna
    position_in_part e chunk_id finali.
    """
    numbered = [
        Paragraph(
            index=i,
            text=p["text"],
            char_start=p["char_start"],
            char_end=p["char_end"],
            part_number=p["part_number"],
            part_title=p["part_title"],
        )
        for i, p in enumerate(paragraphs)
    ]
    drafts, merge_warnings = merge_small_paragraphs(numbered, enc)

    # position_in_part (1-based) per ogni parte
    per_part_counter: dict[int, int] = {}
    finalized: list[dict[str, Any]] = []
    merged_count = 0

    for idx, d in enumerate(drafts):
        per_part_counter[d.part_number] = per_part_counter.get(d.part_number, 0) + 1
        pos_part = per_part_counter[d.part_number]
        chunk_id = f"ch_{idx + 1:04d}"
        if d.merged:
            merged_count += 1
        finalized.append(
            {
                "chunk_id": chunk_id,
                "part": {"number": d.part_number, "title": d.part_title},
                "position_in_part": pos_part,
                "position_global": idx + 1,
                "char_start": d.char_start,
                "char_end": d.char_end,
                "token_count": d.token_count,
                "paragraph_indices": d.paragraph_indices,
                "merged": d.merged,
                "text": d.text,
            }
        )

    # Risolvi chunk_id nei warning (placeholder era indice lista drafts)
    resolved_warnings: list[dict[str, Any]] = []
    for w in merge_warnings:
        ww = dict(w)
        ph = ww.pop("chunk_index_placeholder", None)
        if isinstance(ph, int) and 0 <= ph < len(finalized):
            ww["chunk_id"] = finalized[ph]["chunk_id"]
        resolved_warnings.append(ww)

    return finalized, resolved_warnings, merged_count


def token_stats(values: list[int]) -> dict[str, float | int]:
    """Min, max, media, mediana, p95 (stessa semantica delle richieste di report)."""
    if not values:
        return {"min": 0, "max": 0, "mean": 0.0, "median": 0.0, "p95": 0}
    s = sorted(values)
    n = len(s)
    mean = sum(s) / n
    mid = n // 2
    median = float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0
    rank = min(n - 1, int(math.ceil(0.95 * n)) - 1)
    p95 = float(s[max(0, rank)])
    return {
        "min": s[0],
        "max": s[-1],
        "mean": round(mean, 1),
        "median": round(median, 1),
        "p95": round(p95, 1),
    }


def write_outputs(
    out_dir: Path,
    chunks_payload: dict[str, Any],
    log_payload: dict[str, Any],
) -> None:
    """Scrive chunks.json e chunking_log.json in data/stage_2 (crea directory)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunks.json").write_text(json_dumps_stable(chunks_payload), encoding="utf-8")
    (out_dir / "chunking_log.json").write_text(json_dumps_stable(log_payload), encoding="utf-8")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> None:
    started_at = iso_now()

    cleaned_path = PROJECT_ROOT / CLEANED_TEXT_REL
    raw_path = PROJECT_ROOT / RAW_TEXT_REL
    structure_path = PROJECT_ROOT / STRUCTURE_REL
    out_dir = PROJECT_ROOT / OUTPUT_DIR_REL

    if not cleaned_path.is_file():
        abort(f"File mancante: {cleaned_path}")
    if not structure_path.is_file():
        abort(f"File mancante: {structure_path}")
    if not raw_path.is_file():
        abort(
            f"File mancante: {raw_path} (serve per verificare che cleaned non abbia "
            "alterato gli offset rispetto allo stadio 0)."
        )

    raw_text = raw_path.read_text(encoding="utf-8")
    cleaned_text = cleaned_path.read_text(encoding="utf-8")
    if len(cleaned_text) != len(raw_text):
        abort(
            "Lunghezza cleaned_text diversa da raw_text: gli offset di structure.json "
            f"non sono più validi sullo stadio 1 (len cleaned={len(cleaned_text)}, "
            f"len raw={len(raw_text)}). Aggiorna la pipeline o rigenera cleaned."
        )

    try:
        structure = json.loads(structure_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        abort(f"structure.json non è JSON valido: {e}")

    parts = structure.get("parts")
    if not isinstance(parts, list) or not parts:
        abort("structure.json: campo 'parts' mancante o non lista.")

    total_expected = structure.get("total_chars")
    if total_expected is not None and int(total_expected) != len(cleaned_text):
        abort(
            "Discrepanza tra len(cleaned_text) e structure.json['total_chars']: "
            f"file={len(cleaned_text)}, structure={total_expected}."
        )

    encoding = TOKEN_ENCODER
    try:
        enc = tiktoken.get_encoding(encoding)
    except KeyError:
        abort(f"Encoding tiktoken sconosciuto: {encoding!r}")

    paras_raw = load_paragraphs_with_offsets(cleaned_text)
    paragraphs_total = len(paras_raw)

    paras_with_parts = assign_parts(paras_raw, parts)
    paras_content, excluded_titles = filter_part_titles(paras_with_parts, parts)

    chunks_list, warnings, merged_count = build_chunks(paras_content, enc)
    finished_at = iso_now()

    chunks_by_part: dict[str, int] = {str(i): 0 for i in range(1, 7)}
    for c in chunks_list:
        key = str(c["part"]["number"])
        chunks_by_part[key] = chunks_by_part.get(key, 0) + 1

    tok_values = [int(c["token_count"]) for c in chunks_list]
    stats = token_stats(tok_values)

    chunks_payload: dict[str, Any] = {
        "source": CLEANED_TEXT_REL,
        "structure_source": STRUCTURE_REL,
        "created_at": finished_at,
        "stage_version": STAGE_VERSION,
        "params": {
            "min_tokens": MIN_TOKENS,
            "token_encoder": TOKEN_ENCODER,
            "policy": "1_chunk_per_paragraph_with_small_merge",
        },
        "total_chunks": len(chunks_list),
        "chunks": chunks_list,
    }

    log_payload: dict[str, Any] = {
        "stage_version": STAGE_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "source_files": [CLEANED_TEXT_REL, STRUCTURE_REL],
        "params": {"min_tokens": MIN_TOKENS, "token_encoder": TOKEN_ENCODER},
        "paragraphs_total": paragraphs_total,
        "paragraphs_filtered_as_titles": len(excluded_titles),
        "chunks_produced": len(chunks_list),
        "chunks_merged_count": merged_count,
        "chunks_by_part": chunks_by_part,
        "token_stats": stats,
        "warnings": warnings,
    }

    write_outputs(out_dir, chunks_payload, log_payload)

    # ---- Report stdout -------------------------------------------------
    processed = len(paras_content)
    print(f"Paragrafi totali letti: {paragraphs_total}")
    print(f"Paragrafi filtrati come titoli di parte: {len(excluded_titles)}")
    print(f"Paragrafi processati (contenuto): {processed}")
    print(f"Chunk totali: {len(chunks_list)}, di cui merged flag=True: {merged_count}")
    print("Distribuzione chunk per parte:")
    for k in sorted(chunks_by_part.keys(), key=int):
        print(f"  parte {k}: {chunks_by_part[k]}")
    print(
        "Statistiche token: "
        f"min={stats['min']}, max={stats['max']}, "
        f"median={stats['median']}, p95={stats['p95']}, mean={stats['mean']}"
    )

    def preview_chunk_id(cid: str) -> None:
        found = next((c for c in chunks_list if c["chunk_id"] == cid), None)
        if found:
            t = found["text"][:200].replace("\n", " ")
            print(f"  {cid} (primi 200 caratteri): {t}…")
        else:
            print(f"  {cid}: (non esiste)")

    last_id = f"ch_{len(chunks_list):04d}"
    print("Anteprima chunk a salti:")
    for cid in ("ch_0001", "ch_0050", "ch_0150", last_id):
        if cid == "ch_0050" and len(chunks_list) < 50:
            continue
        if cid == "ch_0150" and len(chunks_list) < 150:
            continue
        preview_chunk_id(cid)

    if warnings:
        print("Warning:")
        for w in warnings:
            print(f"  - {w.get('type')} chunk_id={w.get('chunk_id', '?')}: {w.get('note', w)}")
    else:
        print("Nessun warning.")

    print("File scritti:")
    print(f"  {PROJECT_ROOT / CHUNKS_JSON_REL}")
    print(f"  {PROJECT_ROOT / CHUNKING_LOG_REL}")


if __name__ == "__main__":
    main()
