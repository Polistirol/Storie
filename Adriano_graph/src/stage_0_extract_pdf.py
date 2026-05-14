"""
Stadio 0: estrazione testo dal PDF text-based delle Memorie di Adriano.

Esecuzione (cwd = Adriano_graph/):
    python src/stage_0_extract_pdf.py

Modifica le costanti in cima (IGNORE_PAGES, path, SAVE_INTERMEDIATES) quando serve:
nessun argparse o file di configurazione esterno.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import pdfplumber

# -----------------------------------------------------------------------------
# Costanti modificabili
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PDF_REL = "data/yourcenar-marguerite-memorie-di-adriano.pdf"
OUTPUT_DIR_REL = "data/stage_0"
STAGE_VERSION = "0.2.0"
SAVE_INTERMEDIATES = True

# Tolleranza verticale (pt) per raggruppare token nella stessa riga visiva in extract_words.
LINE_GROUP_TOP_TOLERANCE_PT = 3.0
# Soglia sopra la mediana x0 della pagina (pt): righe con x0 maggiore = probabile inizio paragrafo
# (rientro prima riga). 5pt ~ metà di un rientro tipo 1em a 11pt; conservativo vs oscillazioni layout.
INDENT_TOLERANCE_PT = 5.0
# Se una pagina ha meno di N righe non vuote con x0 noto, la mediana locale è poco significativa:
# si usa la mediana globale del libro.
MIN_NONEMPTY_LINES_FOR_LOCAL_MEDIAN = 3

PARAGRAPH_DETECTION_METHOD = "x0_indentation_v0.2.0"

# Pagine da ignorare (1-indexed). Compilata dopo ispezione: copertina e indice (1-2),
# apparato critico da "TACCUINI DI APPUNTI" in avanti (172-200 in questa edizione).
IGNORE_PAGES = [1, 2,3, *range(171, 201)]

EXPECTED_PART_TITLES: list[str] = [
    "ANIMULA VAGULA BLANDULA",
    "VARIUS MULTIPLEX MULTIFORMIS",
    "TELLUS STABILITA",
    "SAECULUM AUREUM",
    "DISCIPLINA AUGUSTA",
    "PATIENTIA",
]

# Separatore tra pagine nel file intermedio 01 (visivamente separa i blocchi senza usare form feed).
PAGE_SEPARATOR_RAW = "\n\n"
# Dopo lo strip header/footer le pagine vengono unite come linee consecutive del corpo.
PAGE_SEPARATOR_BODY = "\n"


# -----------------------------------------------------------------------------
# Utilità
# -----------------------------------------------------------------------------


def abort(reason: str) -> None:
    print(reason, file=sys.stderr)
    raise SystemExit(1)


def norm_for_header_footer_count(s: str) -> str:
    """Confronto header/footer: collassa spazi come richiesto dalla specifica."""
    return " ".join(s.split())


def format_page_list_compact(sorted_pages: list[int]) -> str:
    if not sorted_pages:
        return "(nessuna)"
    if len(sorted_pages) <= 20:
        return ", ".join(str(p) for p in sorted_pages)
    out: list[str] = []
    start = prev = sorted_pages[0]
    for p in sorted_pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        if start == prev:
            out.append(str(start))
        else:
            out.append(f"{start}-{prev}")
        start = prev = p
    if start == prev:
        out.append(str(start))
    else:
        out.append(f"{start}-{prev}")
    return ", ".join(out)


def json_dumps_stable(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# -----------------------------------------------------------------------------
# Estrazione PDF
# -----------------------------------------------------------------------------

PageLines = list[tuple[str, float | None]]


def _words_to_visual_lines(
    words: list[dict[str, object]], top_tol: float
) -> list[tuple[str, float]]:
    """Raggruppa extract_words per `top` (tolleranza) e costruisce (testo_riga, x0_min)."""
    if not words:
        return []
    # pdfplumber ordina già in reading order con use_text_flow=True; ordine deterministico su top/x0.
    def _t(w: dict[str, object]) -> tuple[float, float]:
        return (float(w["top"]), float(w["x0"]))

    sorted_w = sorted(words, key=_t)
    lines_tokens: list[list[dict[str, object]]] = []
    cur: list[dict[str, object]] = []
    for w in sorted_w:
        t = float(w["top"])
        if not cur:
            cur = [w]
            continue
        mean_top = sum(float(x["top"]) for x in cur) / len(cur)
        if abs(t - mean_top) <= top_tol:
            cur.append(w)
        else:
            lines_tokens.append(cur)
            cur = [w]
    if cur:
        lines_tokens.append(cur)

    out: list[tuple[str, float]] = []
    for row in lines_tokens:
        row_sorted = sorted(row, key=lambda w: float(w["x0"]))
        texts = [str(w["text"]) for w in row_sorted]
        line_text = " ".join(texts).strip()
        x0_min = min(float(w["x0"]) for w in row_sorted)
        out.append((line_text, x0_min))
    return out


def extract_page_lines_with_x0(page: object) -> tuple[PageLines, bool]:
    """
    Estrae righe visive con x0 della prima parola a sinistra.
    Ritorna (righe, fallback_senza_coordinate): se extract_words è vuoto ma c'è testo,
    fallback su extract_text con x0=None per riga.
    """
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
    if words:
        raw_lines = _words_to_visual_lines(words, LINE_GROUP_TOP_TOLERANCE_PT)
        return [(t, x) for t, x in raw_lines], False
    fallback_text = page.extract_text() or ""
    if not fallback_text.strip():
        return [], False
    fb_lines = fallback_text.split("\n")
    return [(ln, None) for ln in fb_lines], True


def extract_pages_payload(
    pdf_path: Path, processed_pages: list[int]
) -> tuple[list[tuple[int, PageLines, bool]], list[int]]:
    """Per ogni pagina: righe (testo, x0) e se è stato usato fallback senza coordinate."""
    pages_out: list[tuple[int, PageLines, bool]] = []
    pages_no_coords: list[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        for idx in sorted(processed_pages):
            lines, no_coords = extract_page_lines_with_x0(pdf.pages[idx - 1])
            pages_out.append((idx, lines, no_coords))
            if no_coords:
                pages_no_coords.append(idx)
    return pages_out, pages_no_coords


def pages_to_plain_text(pages: list[tuple[int, PageLines]]) -> list[tuple[int, str]]:
    return [(pno, "\n".join(t for t, _ in lines)) for pno, lines in pages]


def concat_pages(pages_payload: list[tuple[int, str]], sep: str) -> str:
    return sep.join(text for _p, text in pages_payload)


# -----------------------------------------------------------------------------
# Header / footer ricorrenti
# -----------------------------------------------------------------------------


def detect_recurring_edges(
    pages_payload: list[tuple[int, str]],
    num_processed: int,
) -> tuple[set[str], set[str]]:
    """
    Conta prima/ultima riga per pagina (dopo split su '\\n').
    Soglia strettamente oltre il 30%: floor(n*0.3)+1 occorrenze minime.
    """
    if num_processed == 0:
        return set(), set()
    thr = math.floor(num_processed * 0.3) + 1
    first_ctr: Counter[str] = Counter()
    last_ctr: Counter[str] = Counter()
    for _p, text in pages_payload:
        lines = text.split("\n")
        first = lines[0] if lines else ""
        last = lines[-1] if lines else ""
        first_ctr[norm_for_header_footer_count(first)] += 1
        last_ctr[norm_for_header_footer_count(last)] += 1
    headers = {k for k, c in first_ctr.items() if c >= thr}
    footers = {k for k, c in last_ctr.items() if c >= thr}
    return headers, footers


def detect_recurring_edges(
    pages_payload: list[tuple[int, str]],
    num_processed: int,
) -> tuple[set[str], set[str]]:
    """
    Conta prima/ultima riga per pagina (dopo split su '\\n').
    Soglia strettamente oltre il 30%: floor(n*0.3)+1 occorrenze minime.
    """
    if num_processed == 0:
        return set(), set()
    thr = math.floor(num_processed * 0.3) + 1
    first_ctr: Counter[str] = Counter()
    last_ctr: Counter[str] = Counter()
    for _p, text in pages_payload:
        lines = text.split("\n")
        first = lines[0] if lines else ""
        last = lines[-1] if lines else ""
        first_ctr[norm_for_header_footer_count(first)] += 1
        last_ctr[norm_for_header_footer_count(last)] += 1
    headers = {k for k, c in first_ctr.items() if c >= thr}
    footers = {k for k, c in last_ctr.items() if c >= thr}
    return headers, footers


def strip_detected_edges_lines(
    pages_lines: list[tuple[int, PageLines]],
    header_norms: set[str],
    footer_norms: set[str],
) -> tuple[list[tuple[int, PageLines]], list[str]]:
    """Rimuove solo le righe i cui testi (per norm) coincidono con header/footer ricorrenti."""
    removed_raw: dict[str, None] = {}
    out: list[tuple[int, PageLines]] = []
    for pno, lines in pages_lines:
        work = list(lines)
        while work and norm_for_header_footer_count(work[0][0]) in header_norms:
            removed_raw.setdefault(work[0][0], None)
            work.pop(0)
        while work and norm_for_header_footer_count(work[-1][0]) in footer_norms:
            removed_raw.setdefault(work[-1][0], None)
            work.pop(-1)
        out.append((pno, work))
    removed_sorted = sorted(removed_raw.keys(), key=lambda s: (-len(s), s))
    return out, removed_sorted


# -----------------------------------------------------------------------------
# Normalizzazione caratteri
# -----------------------------------------------------------------------------


def normalize_characters(s: str) -> tuple[str, dict[str, int], int, int]:
    lig_specs = [
        ("\ufb04", "ffl"),
        ("\ufb03", "ffi"),
        ("\ufb00", "ff"),
        ("\ufb01", "fi"),
        ("\ufb02", "fl"),
    ]
    lig_counts = {lbl: 0 for _, lbl in lig_specs}
    out = s
    for uchar, rep in lig_specs:
        cnt = out.count(uchar)
        lig_counts[rep] += cnt
        out = out.replace(uchar, rep)
    nb_before = out.count("\u00a0")
    out = out.replace("\u00a0", " ")
    zn = out.count("\u200b")
    zb = out.count("\ufeff")
    out = out.replace("\u200b", "").replace("\ufeff", "")
    return out, lig_counts, nb_before, zn + zb


def sum_lig_total(lig: dict[str, int]) -> int:
    return int(sum(lig.values()))


# -----------------------------------------------------------------------------
# Sillabazione
# -----------------------------------------------------------------------------


_CONT_WORD = r"[\w\u00c0-\u024f]"
_WORD_START = re.compile(rf"^[ \t]*({_CONT_WORD}+)(.*)$", re.UNICODE)


def resolve_hyphenation(
    lines: list[str],
    line_pages: list[int],
    line_indented: list[bool],
) -> tuple[list[str], list[int], list[bool], list[dict[str, str | int]]]:
    """
    Unisce im-\nperatore eliminando il trattino di fine riga e la newline.
    La pagina loggata è quella della riga che contiene il trattino (iphenation carry).
    """
    events: list[dict[str, str | int]] = []
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(lines) - 1:
            top = lines[i].rstrip()
            if not top.endswith("-") or top.endswith("--"):
                i += 1
                continue
            core = top[:-1]
            if not core or not core[-1].isalnum():
                i += 1
                continue
            nxt = lines[i + 1]
            m = _WORD_START.match(nxt)
            if m is None:
                i += 1
                continue
            cont = m.group(1)
            tail = m.group(2)
            merged = core + cont
            orig_snip = (lines[i].rstrip() + "\n" + lines[i + 1].rstrip())
            if len(orig_snip) > 240:
                orig_snip = orig_snip[:240] + "..."
            events.append(
                {
                    "page": int(line_pages[i]),
                    "original": orig_snip,
                    "resolved": merged,
                }
            )
            new_line = core + cont + tail
            lines[i : i + 2] = [new_line]
            line_pages[i : i + 2] = [line_pages[i]]
            line_indented[i : i + 2] = [line_indented[i]]
            changed = True
            # non incrementare i: riprova sulla stessa riga
        # fine while i
    return lines, line_pages, line_indented, events


# Trattini che possono fungere da inciso o sillabazione; la sillabatura vera è già gestita altrove.
_HYPHEN_LIKE_CHARS = frozenset(("-", "\u2013", "\u2014"))


def detect_hyphen_residue(lines: list[str], line_pages: list[int]) -> list[dict[str, str | int]]:
    """
    Possibili trattini anomali a fine riga, senza interferire con l'inciso tipografico (parola – parola)
    né con parola-\\nminuscola (sillabazione o già ricomposta).

    Non warn se c'è uno spazio (qualsiasi) subito prima del trattino: è inciso, non sillabazione.
    Non warn se la riga seguente, una volta tolti gli spazi iniziali, comincia con minuscola:
    tipico `parola-<newline>minuscola` affrontato dalla sillabazione oppure accettabile nel testo.
    """
    warn: list[dict[str, str | int]] = []
    for i, line in enumerate(lines[:-1]):
        sr = line.rstrip()
        if len(sr) < 2:
            continue
        if sr[-2:] == "--":
            continue
        dch = sr[-1]
        if dch not in _HYPHEN_LIKE_CHARS:
            continue

        prev = sr[-2]
        if prev.isspace():
            continue
        if not prev.isalnum():
            continue

        nxt_trim = lines[i + 1].lstrip()
        if nxt_trim and nxt_trim[0].islower():
            continue

        ctx = (lines[i] + "\n" + lines[i + 1])[:80]
        warn.append(
            {
                "type": "paragraph_ambiguous",
                "page": int(line_pages[i]),
                "context": ctx,
                "note": (
                    "Trattino ASCII/Unicode (-, en, em) a fine riga attaccato a parola dopo "
                    "risoluzione sillabazioni; la continuazione della riga successiva non è minuscola. "
                    "Verificare contenuto editoriale."
                ),
            }
        )
    return warn


# -----------------------------------------------------------------------------
# Paragrafi
# -----------------------------------------------------------------------------


def reconstruct_paragraphs(
    lines: list[str],
    line_pages: list[int],
    is_indented: list[bool],
) -> tuple[str, int, list[dict[str, str | int]], int]:
    """
    Confine paragrafo se riga preceduta da riga vuota, oppure riga con rientro (x0 v.s. mediana).
    Tra paragrafi: due newline; dentro il paragrafo le righe diventano spazio singolo.
    """
    if not (len(lines) == len(line_pages) == len(is_indented)):
        abort("Ricostruzione paragrafi: linee, pagine e flag rientro non allineati.")
    paras: list[str] = []
    cur: list[str] = []
    warnings: list[dict[str, str | int]] = []
    empty_line_paragraph_breaks = 0

    def flush() -> None:
        if not cur:
            return
        inner = " ".join(" ".join(x.split()) for x in cur).strip()
        if inner:
            paras.append(inner)
        cur.clear()

    for idx, ln in enumerate(lines):
        if ln.strip() == "":
            if cur:
                empty_line_paragraph_breaks += 1
            flush()
            continue

        s = ln.strip()
        # I titoli delle sei parti devono restare paragrafi isolati anche se il PDF non
        # inserisce sempre una riga vuota tipografica dopo la riga maiuscola (regola aggiuntiva
        # minima: match esatto alle stringhe canoniche, niente regex generica sui maiuscolati).
        if s in EXPECTED_PART_TITLES:
            flush()
            paras.append(s)
            continue

        # Nuovo paragrafo anche con rientro tipografico (prima riga più a destra del corpo).
        if is_indented[idx] and cur:
            flush()

        cur.append(ln)

    flush()
    final_text = "\n\n".join(paras)
    return final_text, len(paras), warnings, empty_line_paragraph_breaks


def detect_dense_blank_runs(lines: list[str], line_pages: list[int]) -> list[dict[str, str | int]]:
    """Sequenze di 3+ righe vuote: la regola base non definisce comportamento dedicato; log solo."""
    warns: list[dict[str, str | int]] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() != "":
            i += 1
            continue
        j = i
        while j < n and lines[j].strip() == "":
            j += 1
        run_len = j - i
        if run_len >= 3:
            ctx_parts = lines[i : min(i + 3, n)]
            warns.append(
                {
                    "type": "paragraph_ambiguous",
                    "page": int(line_pages[i]),
                    "context": "\n".join(ctx_parts)[:80],
                    "note": (
                        "Tre o piu righe vuote consecutive "
                        f"({run_len}); la regola base le tratta comunque solo come separatore "
                        "singolo di fine paragrafo."
                    ),
                }
            )
        i = j
    return warns


# -----------------------------------------------------------------------------
# Sei parti
# -----------------------------------------------------------------------------


def split_paragraphs(text: str) -> tuple[list[str], list[int]]:
    """
    Lista paragrafi (split su '\\n\\n') e offset carattere iniziale di ciascun blocco
    nel testo finale (per calcolare title_char_start / body ranges).
    """
    if not text:
        return [], []
    parts = text.split("\n\n")
    offsets: list[int] = []
    pos = 0
    for i, blk in enumerate(parts):
        offsets.append(pos)
        pos += len(blk)
        if i < len(parts) - 1:
            pos += 2
    return parts, offsets


def verify_and_build_parts_structure(
    raw_text: str,
) -> tuple[list[dict[str, int | str]], list[dict[str, str | int]]]:
    paragraphs, offs = split_paragraphs(raw_text)
    stripped_blocks = [b.strip() for b in paragraphs]
    found_indexes: dict[str, list[int]] = {t: [] for t in EXPECTED_PART_TITLES}

    for j, blk in enumerate(stripped_blocks):
        if blk in EXPECTED_PART_TITLES:
            found_indexes[blk].append(j)

    msg_parts: list[str] = []

    missing = [t for t in EXPECTED_PART_TITLES if not found_indexes[t]]
    duplicates = [(t, found_indexes[t]) for t in EXPECTED_PART_TITLES if len(found_indexes[t]) > 1]
    if missing:
        msg_parts.append(f"Titoli mancanti come paragrafi isolati: {missing}")
    if duplicates:
        msg_parts.append(f"Titoli duplicati: {duplicates}")

    if msg_parts:
        counts = {
            t: sum(1 for b in stripped_blocks if t in b) for t in EXPECTED_PART_TITLES  # occurrences substring
        }
        msg_parts.append(f"(Substring counts per debugging: {counts})")
        abort(
            "Validazione parti fallita:\n"
            + "\n".join(msg_parts)
            + "\nControllare IGNORE_PAGES: potrebbe includere/indice incompleta."
        )

    order_ok = True
    last_idx = -1
    found_meta: list[dict[str, str | int]] = []
    for num, title in enumerate(EXPECTED_PART_TITLES, start=1):
        j = found_indexes[title][0]
        if j <= last_idx:
            order_ok = False
            break
        last_idx = j
        found_meta.append({"number": num, "title": title, "paragraph_index": j})

    if not order_ok:
        abort(
            "Le sei parti non compaiono nell'ordine atteso nel testo ricomposto.\n"
            "Verificare IGNORE_PAGES o anomalie di impaginazione."
        )

    structure_parts: list[dict[str, int | str]] = []
    for k, title in enumerate(EXPECTED_PART_TITLES):
        j = found_indexes[title][0]
        t0 = offs[j]
        # Esclusiva come slice Python sul blocco salvato nel testo (include eventuali margini nella stringa grezza split).
        t1 = t0 + len(paragraphs[j])
        body_start = t1
        while body_start < len(raw_text) and raw_text[body_start] in "\n ":
            body_start += 1
        if k < 5:
            next_title_j = found_indexes[EXPECTED_PART_TITLES[k + 1]][0]
            body_end = offs[next_title_j]
        else:
            body_end = len(raw_text)
        while body_end > body_start and raw_text[body_end - 1] in "\n ":
            body_end -= 1
        structure_parts.append(
            {
                "number": k + 1,
                "title": title,
                "title_char_start": t0,
                "title_char_end": t1,
                "body_char_start": body_start,
                "body_char_end": body_end,
            }
        )
    return structure_parts, found_meta


def map_titles_to_pdf_pages(
    pages_stripped_hf: list[tuple[int, str]],
) -> dict[str, int]:
    """Trova la pagina PDF del titolo dopo le stesse trasformazioni di testo dell'estrazione."""
    page_map: dict[str, int] = {}
    for title in EXPECTED_PART_TITLES:
        found_page: int | None = None
        for pno, pg in pages_stripped_hf:
            normalized, *_rest = normalize_characters(pg)
            for line in normalized.split("\n"):
                if line.strip() != title:
                    continue
                if found_page is not None:
                    abort(
                        f"Titolo {title} compare piu righe sulla stessa pagina o su piu pagine "
                        f"(prima volta pagina {found_page}, poi {pno})."
                    )
                found_page = pno
        if found_page is None:
            abort(f"Titolo parte non trovato su nessuna pagina normalizzata: {title}")
        page_map[title] = found_page
    return page_map


def flatten_pages_with_indent_and_intermediate(
    stripped_hf_lines: list[tuple[int, PageLines]],
) -> tuple[
    list[str],
    list[int],
    list[bool],
    list[dict[str, object]],
    dict[str, float],
    float,
    list[int],
]:
    """
    Normalizza per pagina (come join+sostituzioni su tutta la pagina), assegna is_indented da x0,
    appiattisce in sequenza globale come split('\\n') sul corpo intero normalizzato.
    Produce anche l'intermediate 00 (per pagina) con flag debug.
    """
    all_x: list[float] = []
    for _pno, plines in stripped_hf_lines:
        for t, x in plines:
            if t.strip() != "" and x is not None:
                all_x.append(float(x))
    global_median_x0 = float(median(all_x)) if all_x else 0.0

    flat_lines: list[str] = []
    flat_pages: list[int] = []
    flat_indent: list[bool] = []
    inter_chunks: list[dict[str, object]] = []
    page_median_x0: dict[str, float] = {}
    pages_using_global_median: list[int] = []

    for pno, plines in stripped_hf_lines:
        if not plines:
            plines = [("", None)]

        raw_join = "\n".join(t for t, _ in plines)
        norm_join, *_ = normalize_characters(raw_join)
        norm_parts = norm_join.split("\n")
        if len(norm_parts) != len(plines):
            abort(
                f"Dopo normalizzazione, pagina {pno}: attese {len(plines)} righe, "
                f"trovate {len(norm_parts)}. Possibile newline interna anomala nel testo."
            )

        x0s_page = [float(x) for (t, x) in plines if t.strip() != "" and x is not None]
        if len(x0s_page) >= MIN_NONEMPTY_LINES_FOR_LOCAL_MEDIAN:
            median_page = float(median(x0s_page))
        else:
            median_page = global_median_x0
            pages_using_global_median.append(pno)
        page_median_x0[str(pno)] = median_page

        threshold = median_page + INDENT_TOLERANCE_PT
        page_inter_lines: list[dict[str, object]] = []

        for (_, x), nt in zip(plines, norm_parts, strict=True):
            flat_lines.append(nt)
            flat_pages.append(int(pno))
            if nt.strip() == "" or x is None:
                ind = False
            else:
                ind = float(x) > threshold
            flat_indent.append(ind)
            page_inter_lines.append(
                {
                    "line": nt,
                    "x0": float(x) if x is not None else None,
                    "is_indented": ind,
                }
            )

        inter_chunks.append({"page": int(pno), "lines": page_inter_lines})

    return (
        flat_lines,
        flat_pages,
        flat_indent,
        inter_chunks,
        page_median_x0,
        global_median_x0,
        pages_using_global_median,
    )


def main() -> None:
    if not IGNORE_PAGES:
        abort("IGNORE_PAGES e' vuota: compila dopo ispezione manuale del PDF, poi rilancia.")

    pdf_path = PROJECT_ROOT / SOURCE_PDF_REL
    if not pdf_path.is_file():
        abort(f"PDF mancante: {pdf_path}")

    started_at = iso_now()

    ignored_pages = sorted(set(IGNORE_PAGES))

    with pdfplumber.open(pdf_path) as pdf:
        pdf_total_pages = len(pdf.pages)

    for pg in ignored_pages:
        if pg < 1 or pg > pdf_total_pages:
            abort(f"IGNORE_PAGES contiene {pg} fuori da 1..{pdf_total_pages}.")

    processed_pages = sorted({p for p in range(1, pdf_total_pages + 1) if p not in set(ignored_pages)})

    k_ign = len(ignored_pages)
    k_proc = len(processed_pages)
    print(
        f"PDF totale: {pdf_total_pages} pagine. Pagine da ignorare: {k_ign}. "
        f"Pagine che processerò: {k_proc}. Elenco pagine processate: {processed_pages}"
    )

    pages_triples, pages_without_coords = extract_pages_payload(pdf_path, processed_pages)
    pages_raw = [
        (pno, "\n".join(t for t, _ in lines)) for pno, lines, _fallback in pages_triples
    ]
    raw_chars_before = sum(len(t) for _p, t in pages_raw)
    raw_concat = concat_pages(pages_raw, PAGE_SEPARATOR_RAW)

    header_norms, footer_norms = detect_recurring_edges(pages_raw, k_proc)
    pages_lines_pre_strip = [(pno, lines) for pno, lines, _f in pages_triples]
    stripped_hf_lines, header_footer_strings = strip_detected_edges_lines(
        pages_lines_pre_strip, header_norms, footer_norms
    )
    stripped_hf = pages_to_plain_text(stripped_hf_lines)
    body_concat_hf = concat_pages(stripped_hf, PAGE_SEPARATOR_BODY)

    title_pdf_pages = map_titles_to_pdf_pages(stripped_hf)

    normalized_text, lig_counts, nb_cnt, zw_cnt = normalize_characters(body_concat_hf)
    global_lines = normalized_text.split("\n")

    (
        rebuilt_lines,
        rebuilt_pages,
        rebuilt_indented,
        lines_x0_intermediate,
        page_median_x0_map,
        global_median_x0,
        pages_using_global_median,
    ) = flatten_pages_with_indent_and_intermediate(stripped_hf_lines)

    if rebuilt_lines != global_lines:
        abort(
            "Inconsistenza tra normalizzazione globale e normalizzazione per pagina: "
            "verificare caratteri a cavallo di fine pagina."
        )

    indented_lines_before_hyphen = int(sum(1 for i, ln in enumerate(rebuilt_lines) if ln.strip() and rebuilt_indented[i]))

    lines_work = rebuilt_lines
    pages_work = rebuilt_pages
    indent_work = rebuilt_indented

    lines_work, pages_work, indent_work, hyphen_events = resolve_hyphenation(
        lines_work, pages_work, indent_work
    )
    hyphen_samples = hyphen_events[:5]
    text_after_hyphen = "\n".join(lines_work)

    warn_hyphen = detect_hyphen_residue(lines_work, pages_work)
    warn_blanks = detect_dense_blank_runs(lines_work, pages_work)
    (
        raw_text,
        n_paras,
        warn_para,
        empty_line_paragraph_breaks,
    ) = reconstruct_paragraphs(lines_work, pages_work, indent_work)
    warnings = warn_hyphen + warn_blanks + warn_para

    structure_parts, _order_meta = verify_and_build_parts_structure(raw_text)

    finished_at = iso_now()

    parts_found_log: list[dict[str, str | int]] = []
    for part in structure_parts:
        tit = str(part["title"])
        parts_found_log.append(
            {
                "number": int(part["number"]),
                "title": tit,
                "found_at_pdf_page": int(title_pdf_pages[tit]),
            }
        )

    header_footer_note = (
        "nessun header/footer ricorrente rilevato"
        if not header_footer_strings
        else "Rimosse righe ricorrenti oltre soglia."
    )

    paragraph_detection_block: dict[str, object] = {
        "method": PARAGRAPH_DETECTION_METHOD,
        "indent_tolerance_pt": float(INDENT_TOLERANCE_PT),
        "median_x0_global": global_median_x0,
        "page_median_x0": page_median_x0_map,
        "pages_using_global_median": sorted(set(pages_using_global_median)),
        "pages_without_coords": sorted(set(pages_without_coords)),
        "indented_lines_count": indented_lines_before_hyphen,
        "empty_line_paragraph_breaks": int(empty_line_paragraph_breaks),
    }

    extraction_log = {
        "stage_version": STAGE_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "source_file": SOURCE_PDF_REL,
        "pdf_total_pages": pdf_total_pages,
        "ignored_pages": ignored_pages,
        "processed_pages": processed_pages,
        "ignored_pages_count": k_ign,
        "processed_pages_count": k_proc,
        "raw_chars_before_processing": raw_chars_before,
        "final_chars": len(raw_text),
        "paragraphs_reconstructed": n_paras,
        "paragraph_detection": paragraph_detection_block,
        "hyphenation_resolutions": {
            "count": len(hyphen_events),
            "samples": hyphen_samples,
            "all": hyphen_events,
        },
        "ligatures_normalized": lig_counts,
        "ligatures_normalized_total": sum_lig_total(lig_counts),
        "nbsp_replaced": nb_cnt,
        "zero_width_removed": zw_cnt,
        "header_footer_removed": header_footer_strings,
        "header_footer_note": header_footer_note,
        "parts_found": parts_found_log,
        "warnings": warnings,
    }

    structure = {
        "source_file": SOURCE_PDF_REL,
        "extracted_at": finished_at,
        "stage_version": STAGE_VERSION,
        "paragraph_detection_method": PARAGRAPH_DETECTION_METHOD,
        "pdf_total_pages": pdf_total_pages,
        "ignored_pages": ignored_pages,
        "processed_pages": processed_pages,
        "total_chars": len(raw_text),
        "total_paragraphs": n_paras,
        "parts": structure_parts,
    }

    out_dir = PROJECT_ROOT / OUTPUT_DIR_REL
    inter_dir = out_dir / "intermediates"
    out_dir.mkdir(parents=True, exist_ok=True)
    if SAVE_INTERMEDIATES:
        inter_dir.mkdir(parents=True, exist_ok=True)
        (inter_dir / "00_lines_with_x0.json").write_text(
            json_dumps_stable(lines_x0_intermediate), encoding="utf-8", newline="\n"
        )
        (inter_dir / "01_raw_concat.txt").write_text(raw_concat, encoding="utf-8", newline="\n")
        (inter_dir / "02_after_ligatures.txt").write_text(normalized_text, encoding="utf-8", newline="\n")
        (inter_dir / "03_after_hyphen_join.txt").write_text(text_after_hyphen, encoding="utf-8", newline="\n")
        (inter_dir / "04_after_paragraph_join.txt").write_text(raw_text, encoding="utf-8", newline="\n")

    (out_dir / "raw_text.txt").write_text(raw_text, encoding="utf-8", newline="\n")
    (out_dir / "structure.json").write_text(json_dumps_stable(structure), encoding="utf-8", newline="\n")
    (out_dir / "extraction_log.json").write_text(json_dumps_stable(extraction_log), encoding="utf-8", newline="\n")

    # Report finale (solo quanto richiesto dalla specifica)
    para_amb = sum(1 for w in warnings if w.get("type") == "paragraph_ambiguous")
    paras_list, _offs = split_paragraphs(raw_text)

    def _para_preview(i: int) -> str:
        if i < 0 or i >= len(paras_list):
            return ""
        return paras_list[i][:200]

    first_three_pages_medians = [
        (p, float(page_median_x0_map[str(p)])) for p in processed_pages[:3] if str(p) in page_median_x0_map
    ]
    print()
    print(
        f"Pagine totali nel PDF: {pdf_total_pages}. "
        f"Pagine ignorate: {k_ign}. Pagine processate: {k_proc} "
        f"({format_page_list_compact(processed_pages)})."
    )
    print(f"Elenco esatto pagine processate: {processed_pages}")
    print(f"Caratteri totali in raw_text.txt: {len(raw_text)}. Paragrafi totali: {n_paras}.")
    print(
        f"Paragrafi (verifica split): totale={len(paras_list)}. "
        f"Mediana globale x0={global_median_x0:.2f} pt; "
        f"pagine con fallback mediana globale: {len(set(pages_using_global_median))}."
    )
    print(f"Mediane x0 prime 3 pagine processate: {first_three_pages_medians}")
    if len(paras_list) >= 3:
        print(f"Terzo paragrafo (primi 200 caratteri): {_para_preview(2)!r}")
    print(
        "Campionamento paragrafi #1, #3, #10, #50 (primi 200 caratteri ciascuno): "
        f"{_para_preview(0)!r} | {_para_preview(2)!r} | {_para_preview(9)!r} | {_para_preview(49)!r}"
    )
    print(
        "Sei parti (ordine, pagina PDF inizio, offset titolo nel raw_text, primi 120 caratteri del corpo):"
    )
    for part in structure_parts:
        tit = str(part["title"])
        bs = int(part["body_char_start"])
        be = int(part["body_char_end"])
        body_snip = raw_text[bs:be][:120]
        pdf_p = int(title_pdf_pages[tit])
        t0 = int(part["title_char_start"])
        print(f"  {part['number']}. {tit} — PDF p.{pdf_p}, char_start={t0} — {body_snip!r}")
    print(
        f"Legature normalizzate (totale caratteri sostituiti): {sum_lig_total(lig_counts)}. "
        f"Sillabazioni risolte: {len(hyphen_events)}. "
        f"Warning paragraph_ambiguous: {para_amb}."
    )
    print(f"Righe marcate indentate (prima della sillabazione): {indented_lines_before_hyphen}.")
    print(f"Salti paragrafo da riga vuota: {empty_line_paragraph_breaks}.")
    print(f"Pagine senza coordinate (fallback extract_text): {sorted(set(pages_without_coords))}.")
    print(f"Primi 500 caratteri di raw_text.txt:\n{raw_text[:500]!r}")
    print(f"Ultimi 500 caratteri di raw_text.txt:\n{raw_text[-500:]!r}")


if __name__ == "__main__":
    main()
