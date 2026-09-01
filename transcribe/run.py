"""Orchestratore della pipeline di trascrizione: stadio 0 -> 1 -> 2 -> 3 (opzionale).

Esegue in sequenza ingestion (ffmpeg), trascrizione (WhisperX), formattazione
del dialogo e, se richiesto, riassunto LLM.

Esecuzione (env adriano-transcribe attivo, cwd = transcribe/):
    python run.py
    python run.py --config config.yaml
    python run.py --input resources/raw_audio/test1.mp3   # un singolo file (path relativo alla cwd)
    python run.py --from 1            # riparte dallo stadio 1 (salta l'ingestion)
    python run.py --from 3            # solo riassunto LLM (chiede conferma e backend)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import stage_0_ingest
import stage_1_transcribe
import stage_1b_refine
import stage_2_dialogue
import stage_3_recap
from common import format_duration
from config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline di trascrizione audio->dialogo (WhisperX).")
    parser.add_argument("--config", type=Path, default=None, help="Path a config.yaml")
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Singolo file audio da processare (path relativo alla cwd). Default: tutta input_dir.",
    )
    parser.add_argument(
        "--from",
        dest="start_stage",
        type=int,
        default=0,
        choices=(0, 1, 2, 3),
        help="Stadio iniziale (0=ingest, 1=transcribe, 2=dialogue, 3=recap). Default: 0",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)

    # Path passato da CLI: risolto rispetto alla cwd (non a transcribe/).
    input_path = args.input.resolve() if args.input is not None else None
    # Con --input la pipeline lavora SOLO su quel file, in tutti gli stadi.
    only_stem = input_path.stem if input_path is not None else None

    t_pipeline = time.perf_counter()

    if args.start_stage <= 0:
        stage_0_ingest.run(cfg, input_path)
    if args.start_stage <= 1:
        stage_1_transcribe.run(cfg, only_stem)
        if cfg.refine_speakers:
            stage_1b_refine.run(cfg, only_stem)
    if args.start_stage <= 2:
        stage_2_dialogue.run(cfg, only_stem)

    if args.start_stage <= 3:
        stage_3_recap.prompt_and_run(cfg, only_stem)

    print(
        f"\nPipeline completata in {format_duration(time.perf_counter() - t_pipeline)}. "
        f"Dialoghi in: {cfg.stage2_dir}"
    )


if __name__ == "__main__":
    main()
