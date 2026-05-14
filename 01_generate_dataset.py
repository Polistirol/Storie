"""
Estrae testo da un ePub e genera un dataset JSONL di coppie domanda/risposta
tramite Anthropic Claude.

Dipendenze: ebooklib, beautifulsoup4, anthropic
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

import anthropic
from bs4 import BeautifulSoup
from ebooklib import epub

# --- Configurazione ---
EPUB_PATH = "resources/epubs/adriano.epub"
OUTPUT_PATH = "output/dataset_qa.jsonl"
MAX_CHUNK_TOKENS = 800
OVERLAP_TOKENS = 100
PAIRS_PER_CHUNK = 10
from dotenv import load_dotenv
load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Pagine da NON analizzare (ordine di lettura = spine).
# - SKIP_SPINE_ITEM_IDS: id nella spine/manifest (es. da content.opf, attributo id).
# - SKIP_ITEM_NAME_SUBSTRINGS: ignora il documento se il path interno contiene una di queste
#   sottostringhe (case-insensitive), es. "copyright", "indice", "note_redazione".
SKIP_SPINE_ITEM_IDS: frozenset[str] = frozenset({
"cover",
"nav",
"chapter_0",
"chapter_1",
"chapter_2",
"chapter_3",
#"chapter_4",
#"chapter_18",
#"chapter_56",
#"chapter_89",
#"chapter_123",
#"chapter_157",
"chapter_171",
"chapter_170",
"chapter_171",
"chapter_172",
"chapter_173",
"chapter_174",
"chapter_175",
"chapter_176",
"chapter_177",
"chapter_178",
"chapter_179",
"chapter_180",
"chapter_181",
"chapter_182",
"chapter_183",
"chapter_184",
"chapter_185",
"chapter_186",
"chapter_187",
"chapter_188",
"chapter_189",
"chapter_190",
"chapter_191",
"chapter_192",
"chapter_193",
"chapter_194",
"chapter_195",
"chapter_196",
"chapter_197",
"chapter_198",
"chapter_199",
"chapter_200",
})

SKIP_ITEM_NAME_SUBSTRINGS: tuple[str, ...] = ()

# Se il primo JSON è malformato (virgolette non escapate, ecc.), una seconda chiamata chiede solo la correzione sintattica.
REPAIR_JSON_ON_PARSE_ERROR = True

#MODEL_ID = "claude-sonnet-4-20250514"
MODEL_ID = "claude-sonnet-4-6"
SYSTEM_PROMPT = (
    "Sei un assistente che genera dataset per fine-tuning LLM. "
    "Rispondi SOLO con JSON valido, nessun testo aggiuntivo. "
    "Nelle chiavi instruction e response le virgolette doppie interne al testo vanno escapate come \\\"."
)

REPAIR_SYSTEM_PROMPT = (
    "Sei un correttore di sintassi JSON. Rispondi SOLO con JSON valido, nessun testo aggiuntivo. "
    "Non modificare il significato delle stringhe; sistema solo escape, virgolette e virgole "
    "in modo che json.loads funzioni."
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _item_should_be_skipped(item_id: str, item_name: str | None) -> bool:
    if item_id in SKIP_SPINE_ITEM_IDS:
        return True
    name_lower = (item_name or "").lower()
    return any(s.lower() in name_lower for s in SKIP_ITEM_NAME_SUBSTRINGS if s)


_HTML_MEDIA_TYPES = frozenset({
    "application/xhtml+xml",
    "application/html",
    "text/html",
    "text/xhtml",
})
_HTML_SUFFIXES = (".html", ".xhtml", ".htm")


def _spine_item_is_html_document(item: epub.EpubItem) -> bool:
    """Alcuni ePub segnano get_type()==0 per quasi tutto; il MIME e l'estensione sono più affidabili."""
    mt = (getattr(item, "media_type", None) or "").strip().lower()
    if mt.startswith("image/"):
        return False
    if mt in _HTML_MEDIA_TYPES:
        return True
    name = (item.get_name() or "").lower()
    return name.endswith(_HTML_SUFFIXES)


def strip_epub_noise_from_soup(soup: BeautifulSoup) -> None:
    """Rimuove markup non narrativo (titolo 'Page N', pagebreak epub, numeri pagina comuni)."""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all("title"):
        tag.decompose()
    for tag in soup.find_all(attrs={"epub:type": True}):
        et = str(tag.get("epub:type") or "").lower()
        if "pagebreak" in et:
            tag.decompose()
    for sel in (".page-number", ".pagenum", ".page-num", ".pagebreak", "span.page", ".page"):
        for tag in soup.select(sel):
            tag.decompose()


def extract_body_paragraph_text(soup: BeautifulSoup) -> str:
    """Solo testo dei <p> dentro <body>; le <img> (anche dentro un <p>) non contribuiscono."""
    for img in soup.find_all("img"):
        img.decompose()
    body = soup.find("body")
    root = body if body is not None else soup
    chunks: list[str] = []
    for p in root.find_all("p"):
        t = p.get_text(separator=" ", strip=True)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            chunks.append(t)
    return " ".join(chunks)


def clean_full_text(text: str) -> str:
    """Pulizia sul testo già unito: rimuove marcatori tipo 'Page 12' rimasti nel flusso e normalizza spazi."""
    text = re.sub(r"\bPage\s+\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_clean_text_from_epub(epub_path: str) -> str:
    """Legge l'ePub seguendo la spine e restituisce testo senza HTML."""
    book = epub.read_epub(epub_path)
    parts: list[str] = []
    for spine_entry in book.spine:
        item_id = spine_entry[0] if isinstance(spine_entry, tuple) else spine_entry
        item = book.get_item_with_id(item_id)
        if item is None or not _spine_item_is_html_document(item):
            continue
        item_name = item.get_name()
        if _item_should_be_skipped(item_id, item_name):
            logger.info("Salto documento spine id=%r nome=%r", item_id, item_name)
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        strip_epub_noise_from_soup(soup)
        text = extract_body_paragraph_text(soup)
        if text:
            parts.append(text)
    merged = "\n\n".join(parts)
    # Save the merged variable to a .txt file with the same base name as the input epub_path
    import os
    txt_path = os.path.splitext(epub_path)[0] + ".txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(merged)
    return clean_full_text(merged)


def chunk_text_by_words(text: str, max_words: int, overlap_words: int) -> list[str]:
    """Divide il testo in chunk con overlap (parole ≈ token)."""
    words = text.split()
    if not words:
        return []
    if max_words <= 0:
        return [" ".join(words)]
    overlap_words = max(0, min(overlap_words, max_words - 1))
    step = max(1, max_words - overlap_words)
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += step
    return chunks


def build_user_prompt(chunk: str, pairs_per_chunk: int) -> str:
    return (
        "Dal seguente testo scritto in prima persona, genera "
        f"{pairs_per_chunk} coppie domanda/risposta come se stessi intervistando "
        "il narratore. Le risposte devono essere in prima persona, fedeli al testo, "
        "con lo stesso stile e tono. "
        'Formato: {"pairs": [{"instruction": "...", "response": "..."}]}\n\n'
        "TESTO:\n"
        f"{chunk}"
    )


def _strip_markdown_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _normalize_json_quotes(s: str) -> str:
    return (
        s.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )


def _remove_trailing_commas_json(s: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _extract_balanced_object(raw: str) -> str:
    """Taglia il primo oggetto `{ ... }` rispettando stringhe JSON tra doppie virgolette."""
    cleaned = _strip_markdown_fence(raw).strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("Nessun oggetto JSON nella risposta")
    depth = 0
    in_str = False
    escape = False
    i = start
    while i < len(cleaned):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return cleaned[start : i + 1]
        i += 1
    raise ValueError("JSON oggetto non bilanciato")


def _loads_pairs_root(raw_text: str) -> dict[str, Any]:
    """Prova a parsare l'oggetto radice con alcuni tentativi di sanificazione leggera."""
    blobs = []
    try:
        blobs.append(_extract_balanced_object(raw_text))
    except ValueError:
        blobs.append(_strip_markdown_fence(raw_text).strip())

    last_err: json.JSONDecodeError | None = None
    for blob in blobs:
        variants = [
            blob,
            _normalize_json_quotes(blob),
            _remove_trailing_commas_json(_normalize_json_quotes(blob)),
        ]
        for cand in variants:
            try:
                data = json.loads(cand)
            except json.JSONDecodeError as e:
                last_err = e
                continue
            if isinstance(data, dict):
                return data
    if last_err is not None:
        raise last_err
    raise json.JSONDecodeError("Impossibile parsare JSON", raw_text, 0)


def parse_pairs_json(raw_text: str) -> list[dict[str, str]]:
    """Estrae la lista di coppie dalla risposta modello."""
    data = _loads_pairs_root(raw_text)
    pairs_raw = data.get("pairs")
    if not isinstance(pairs_raw, list):
        raise ValueError("Manca la chiave 'pairs' o non è una lista")
    out: list[dict[str, str]] = []
    for item in pairs_raw:
        if not isinstance(item, dict):
            continue
        inst = item.get("instruction")
        resp = item.get("response")
        if isinstance(inst, str) and isinstance(resp, str):
            out.append({"instruction": inst.strip(), "response": resp.strip()})
    return out


def anthropic_repair_json(client: anthropic.Anthropic, broken_raw: str) -> str:
    """Seconda chiamata: correggere solo la sintassi JSON."""
    user_prompt = (
        "Il testo seguente doveva essere un unico oggetto JSON con chiave \"pairs\": "
        'lista di oggetti con chiavi "instruction" e "response". '
        "Non è valido per json.loads (virgolette non escapate, virgole extra, testo fuori JSON, ecc.). "
        "Rispondi SOLO con l'oggetto JSON corretto, senza markdown.\n\n"
        f"{broken_raw}"
    )
    message = client.messages.create(
        model=MODEL_ID,
        max_tokens=8192,
        system=REPAIR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    blocks = getattr(message, "content", None) or []
    texts: list[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            texts.append(getattr(block, "text", "") or "")
    fixed = "".join(texts).strip()
    if not fixed:
        raise ValueError("Riparazione JSON: risposta vuota")
    return fixed


def call_anthropic_for_pairs(
    client: anthropic.Anthropic, chunk: str, pairs_per_chunk: int
) -> list[dict[str, str]]:
    user_prompt = build_user_prompt(chunk, pairs_per_chunk)
    message = client.messages.create(
        model=MODEL_ID,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    blocks = getattr(message, "content", None) or []
    texts: list[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            texts.append(getattr(block, "text", "") or "")
    raw = "".join(texts).strip()
    if not raw:
        raise ValueError("Risposta vuota dal modello")
    try:
        return parse_pairs_json(raw)
    except (json.JSONDecodeError, ValueError):
        if not REPAIR_JSON_ON_PARSE_ERROR:
            raise
        logger.warning("JSON non parsabile dalla prima risposta; richiesta riparazione al modello…")
        fixed = anthropic_repair_json(client, raw)
        return parse_pairs_json(fixed)

def write_jsonl(path: str, rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

def write_chunks(chunks: list[str], chunks_dir: str) -> None:
    os.makedirs(chunks_dir, exist_ok=True)
    for idx, chunk in enumerate(chunks, start=1):
        chunk_path = os.path.join(chunks_dir, f"chunk_{idx:04d}.txt")
        with open(chunk_path, "w", encoding="utf-8") as f:
            f.write(chunk)
    logger.info("Chunks salvati in %s", chunks_dir)


def main() -> None:
    if not ANTHROPIC_API_KEY:
        logger.error("Variabile d'ambiente ANTHROPIC_API_KEY non impostata.")
        sys.exit(1)
    if not os.path.isfile(EPUB_PATH):
        logger.error("File ePub non trovato: %s", EPUB_PATH)
        sys.exit(1)

    logger.info("Lettura ePub: %s", EPUB_PATH)
    full_text = extract_clean_text_from_epub(EPUB_PATH)
    if not full_text.strip():
        logger.error("Nessun testo estratto dall'ePub.")
        sys.exit(1)

    chunks = chunk_text_by_words(full_text, MAX_CHUNK_TOKENS, OVERLAP_TOKENS)
    total_chunks = len(chunks)
    logger.info("Chunk totali: %d (max parole ~ %d, overlap ~ %d)", total_chunks, MAX_CHUNK_TOKENS, OVERLAP_TOKENS)

    write_chunks(chunks, "resources/chunks/adriano")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_pairs: list[dict[str, str]] = []

    for i, chunk in enumerate(chunks, start=1):
        print(f"Chunk {i} di {total_chunks}… (coppie finora: {len(all_pairs)})")
        try:
            pairs = call_anthropic_for_pairs(client, chunk, PAIRS_PER_CHUNK)
            all_pairs.extend(pairs)
        except anthropic.APIError as e:
            logger.error("Errore API Anthropic sul chunk %d/%d: %s", i, total_chunks, e)
            continue
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.error("Errore parsing risposta sul chunk %d/%d: %s", i, total_chunks, e)
            continue
        except Exception as e:
            logger.error("Errore imprevisto sul chunk %d/%d: %s", i, total_chunks, e)
            continue

    write_jsonl(OUTPUT_PATH, all_pairs)
    print(f"Completato. Coppie totali generate: {len(all_pairs)} → salvate in {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
