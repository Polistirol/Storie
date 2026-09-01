"""Stadio 0: ingestion e normalizzazione audio.

Converte qualsiasi formato di input (mp3, wav, m4a, flac, ogg, mp4, ...) nel
formato richiesto dagli stadi successivi: WAV PCM 16-bit, mono, 16 kHz, con
normalizzazione di loudness opzionale (EBU R128). La decodifica e' delegata a
ffmpeg, quindi i formati supportati sono quelli di ffmpeg.

Esecuzione (env attivo, cwd = transcribe/):
    python src/stage_0_ingest.py                 # processa tutti i file in resources/raw_audio
    python src/stage_0_ingest.py --input resources/raw_audio/test1.mp3
    python src/stage_0_ingest.py --config config.yaml

Output:
    data/stage_0_ingest/<nome>.wav
    data/stage_0_ingest/ingest_log.json
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import (
    discover_audio_files,
    format_duration,
    get_logger,
    iso_now,
    probe_duration_seconds,
    require_tool,
    run_command,
    write_json,
)
from config import TranscribeConfig, load_config

STAGE_VERSION = "0.1.0"
logger = get_logger(__name__)


def build_ffmpeg_command(
    ffmpeg: str, src: Path, dst: Path, cfg: TranscribeConfig
) -> list[str]:
    cmd = [
        ffmpeg,
        "-y",                       # sovrascrivi output
        "-i", str(src),
        "-vn",                      # scarta eventuale traccia video (mp4/mkv)
        "-ac", str(cfg.target_channels),
        "-ar", str(cfg.target_sample_rate),
    ]
    if cfg.loudnorm:
        cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    cmd += [
        "-c:a", "pcm_s16le",        # WAV PCM 16-bit
        str(dst),
    ]
    return cmd


def convert_one(ffmpeg: str, src: Path, out_dir: Path, cfg: TranscribeConfig) -> dict:
    dst = out_dir / f"{src.stem}.wav"
    cmd = build_ffmpeg_command(ffmpeg, src, dst, cfg)
    proc = run_command(cmd)
    ok = proc.returncode == 0 and dst.is_file()

    entry: dict[str, object] = {
        "source": src.name,
        "output": dst.name,
        "ok": ok,
        "source_duration_s": probe_duration_seconds(src),
    }
    if ok:
        entry["output_duration_s"] = probe_duration_seconds(dst)
        logger.info("OK  %s -> %s", src.name, dst.name)
    else:
        entry["error"] = (proc.stderr or "").strip()[-500:]
        logger.error("FALLITO %s: %s", src.name, entry["error"])
    return entry


def run(cfg: TranscribeConfig, input_path: Path | None = None) -> list[dict]:
    ffmpeg = require_tool("ffmpeg")
    out_dir = cfg.stage0_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if input_path is not None:
        if not input_path.is_file():
            raise SystemExit(f"File di input inesistente: {input_path}")
        sources = [input_path]
    else:
        sources = discover_audio_files(cfg.input_dir, cfg.input_extensions)

    if not sources:
        raise SystemExit(
            f"Nessun file audio trovato in {cfg.input_dir} "
            f"(estensioni accettate: {', '.join(cfg.input_extensions)})."
        )

    started_at = iso_now()
    t0 = time.perf_counter()
    results = [convert_one(ffmpeg, src, out_dir, cfg) for src in sources]
    elapsed = time.perf_counter() - t0
    n_ok = sum(1 for r in results if r["ok"])

    write_json(
        out_dir / "ingest_log.json",
        {
            "stage_version": STAGE_VERSION,
            "started_at": started_at,
            "finished_at": iso_now(),
            "elapsed_s": round(elapsed, 1),
            "input_dir": str(cfg.input_dir),
            "target_sample_rate": cfg.target_sample_rate,
            "target_channels": cfg.target_channels,
            "loudnorm": cfg.loudnorm,
            "files_total": len(results),
            "files_ok": n_ok,
            "results": results,
        },
    )

    print(
        f"\nStadio 0 completato in {format_duration(elapsed)}: "
        f"{n_ok}/{len(results)} file convertiti -> {out_dir}"
    )
    if n_ok < len(results):
        raise SystemExit("Alcune conversioni sono fallite (vedi ingest_log.json).")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Stadio 0: ingestion/normalizzazione audio (ffmpeg).")
    parser.add_argument("--config", type=Path, default=None, help="Path a config.yaml")
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Singolo file audio (path relativo alla cwd). Default: tutta input_dir.",
    )
    args = parser.parse_args()
    input_path = args.input.resolve() if args.input is not None else None
    run(load_config(args.config), input_path)


if __name__ == "__main__":
    main()
