"""Stadio 2: formattazione del dialogo A/B.

Trasforma il JSON dello stadio 1 (segmenti con speaker e timestamp) in un dialogo
leggibile. Gli speaker grezzi di pyannote (SPEAKER_00, SPEAKER_01, ...) sono
rimappati su etichette leggibili in ordine di comparsa (primo che parla -> "A").
I turni consecutivi dello stesso speaker vengono uniti in un unico blocco con
intervallo [inizio - fine] se derivano da piu' segmenti ASR.

Nota: questo stadio NON corregge ne' riscrive il testo (nessun LLM). Produce la
trascrizione fedele, pronta per un'eventuale rifinitura successiva.

Esecuzione (env attivo, cwd = transcribe/):
    python src/stage_2_dialogue.py
    python src/stage_2_dialogue.py --config config.yaml

Output:
    data/stage_2_dialogue/<nome>.md
    data/stage_2_dialogue/<nome>.txt
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import (
    format_duration,
    format_timestamp,
    get_logger,
    iso_now,
    read_json,
    write_json,
    write_text,
)
from config import TranscribeConfig, load_config

STAGE_VERSION = "0.1.0"
logger = get_logger(__name__)

UNKNOWN_LABEL = "?"


def build_speaker_map(segments: list[dict], labels: tuple[str, ...]) -> dict[str, str]:
    """Mappa gli speaker grezzi su etichette leggibili in ordine di comparsa."""
    mapping: dict[str, str] = {}
    for seg in segments:
        spk = seg.get("speaker")
        if not spk or spk in mapping:
            continue
        idx = len(mapping)
        mapping[spk] = labels[idx] if idx < len(labels) else f"S{idx + 1}"
    return mapping


def merge_turns(segments: list[dict], speaker_map: dict[str, str]) -> list[dict]:
    """Unisce segmenti consecutivi dello stesso speaker in un unico turno."""
    turns: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        label = speaker_map.get(seg.get("speaker"), UNKNOWN_LABEL)
        start = seg.get("start")
        end = seg.get("end")

        if turns and turns[-1]["label"] == label:
            last = turns[-1]
            last["text"] = f"{last['text']} {text}".strip()
            if end is not None:
                last["end"] = end
            last["parts"] += 1
            continue

        turns.append(
            {"label": label, "start": start, "end": end, "text": text, "parts": 1}
        )
    return turns


def format_turn_timestamp(turn: dict) -> str:
    """Timestamp singolo o intervallo [inizio - fine] se il turno unisce piu' segmenti."""
    start = turn.get("start")
    if start is None:
        return ""
    start_s = format_timestamp(start)
    if turn.get("parts", 1) > 1 and turn.get("end") is not None:
        return f" [{start_s} - {format_timestamp(turn['end'])}]"
    return f" [{start_s}]"


def render_markdown(
    turns: list[dict], source: str, include_timestamps: bool
) -> str:
    lines = [f"# Dialogo — {source}", ""]
    for turn in turns:
        ts = format_turn_timestamp(turn) if include_timestamps else ""
        lines.append(f"**{turn['label']}**{ts}: {turn['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_plaintext(turns: list[dict]) -> str:
    return "\n".join(f"{t['label']}: {t['text']}" for t in turns) + "\n"


def format_one(json_path: Path, cfg: TranscribeConfig, out_dir: Path) -> dict:
    data = read_json(json_path)
    segments = data.get("segments", [])
    speaker_map = build_speaker_map(segments, cfg.speaker_labels)
    turns = merge_turns(segments, speaker_map)

    stem = json_path.stem
    source = data.get("source_wav", stem)
    write_text(
        out_dir / f"{stem}.md",
        render_markdown(turns, source, cfg.include_timestamps),
    )
    write_text(out_dir / f"{stem}.txt", render_plaintext(turns))

    logger.info("[%s] %d turni, speaker: %s", stem, len(turns), speaker_map)
    return {
        "file": stem,
        "turns": len(turns),
        "speaker_map": speaker_map,
    }


def run(cfg: TranscribeConfig, only_stem: str | None = None) -> list[dict]:
    # Legge i JSON rifiniti (stadio 1b) se la rifinitura e' attiva, altrimenti quelli grezzi.
    source_dir = cfg.stage2_source_dir
    log_names = {"transcribe_log.json", "refine_log.json"}
    if only_stem is not None:
        target = source_dir / f"{only_stem}.json"
        if not target.is_file():
            raise SystemExit(
                f"JSON atteso non trovato: {target}. Esegui prima gli stadi precedenti su quel file."
            )
        jsons = [target]
    else:
        jsons = sorted(p for p in source_dir.glob("*.json") if p.name not in log_names)
    if not jsons:
        raise SystemExit(
            f"Nessun JSON in {source_dir}. Esegui prima gli stadi precedenti."
        )

    out_dir = cfg.stage2_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = iso_now()
    t0 = time.perf_counter()
    results = [format_one(p, cfg, out_dir) for p in jsons]
    elapsed = time.perf_counter() - t0

    write_json(
        out_dir / "dialogue_log.json",
        {
            "stage_version": STAGE_VERSION,
            "started_at": started_at,
            "finished_at": iso_now(),
            "elapsed_s": round(elapsed, 1),
            "speaker_labels": list(cfg.speaker_labels),
            "results": results,
        },
    )

    print(
        f"\nStadio 2 completato in {format_duration(elapsed)}: "
        f"{len(results)} dialoghi -> {out_dir}"
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Stadio 2: formattazione dialogo A/B.")
    parser.add_argument("--config", type=Path, default=None, help="Path a config.yaml")
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Processa solo questo file (nome senza estensione). Default: tutti.",
    )
    args = parser.parse_args()
    run(load_config(args.config), args.only)


if __name__ == "__main__":
    main()
