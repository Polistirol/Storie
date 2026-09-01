"""Stadio 1: trascrizione con WhisperX (ASR + allineamento + diarizzazione).

Per ogni WAV normalizzato dallo stadio 0 esegue, in sequenza per contenere la VRAM:
  1. ASR        - faster-whisper (large-v3) tramite whisperx.load_model
  2. Alignment  - wav2vec2 (it: VOXPOPULI_ASR_BASE_10K_IT) per timestamp a livello di parola
  3. Diarizione - pyannote (speaker-diarization-community-1) per separare le voci
  4. Fusione    - assegna ogni parola/segmento a uno speaker

La diarizzazione richiede un token HuggingFace (variabile d'ambiente HF_TOKEN o
nel .env della root) e l'accettazione delle condizioni del modello pyannote.
Disattivabile con diarize: false in config.yaml.

Esecuzione (env attivo, cwd = transcribe/):
    python src/stage_1_transcribe.py
    python src/stage_1_transcribe.py --config config.yaml

Output:
    data/stage_1_transcribe/<nome>.json
    data/stage_1_transcribe/transcribe_log.json
"""

from __future__ import annotations

import argparse
import gc
import time
from pathlib import Path

from common import (
    configure_model_cache,
    format_duration,
    get_logger,
    iso_now,
    load_hf_token,
    patch_speechbrain_compat,
    probe_duration_seconds,
    quiet_noisy_logs,
    write_json,
)
from config import TranscribeConfig, load_config

STAGE_VERSION = "0.1.0"
logger = get_logger(__name__)


def _free_gpu(model: object | None = None) -> None:
    """Libera la VRAM tra un modello e l'altro (importante su 3060)."""
    import torch

    if model is not None:
        del model
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _clean_word(word: dict) -> dict:
    out: dict[str, object] = {"word": word.get("word", "")}
    for key in ("start", "end", "score", "speaker"):
        if key in word and word[key] is not None:
            out[key] = word[key]
    return out


def _clean_segments(segments: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for seg in segments:
        cleaned.append(
            {
                "start": seg.get("start"),
                "end": seg.get("end"),
                "speaker": seg.get("speaker"),
                "text": (seg.get("text") or "").strip(),
                "words": [_clean_word(w) for w in seg.get("words", [])],
            }
        )
    return cleaned


def _count_speakers(segments: list[dict]) -> list[str]:
    seen: list[str] = []
    for seg in segments:
        spk = seg.get("speaker")
        if spk and spk not in seen:
            seen.append(spk)
    return seen


def transcribe_one(wav: Path, cfg: TranscribeConfig, hf_token: str | None) -> dict:
    import whisperx

    logger.info("[%s] caricamento audio", wav.name)
    audio = whisperx.load_audio(str(wav))

    # 1) ASR -------------------------------------------------------------------
    logger.info("[%s] ASR (%s, %s)", wav.name, cfg.whisper_model, cfg.compute_type)
    asr_model = whisperx.load_model(
        cfg.whisper_model,
        device=cfg.device,
        compute_type=cfg.compute_type,
        language=cfg.language,
        download_root=str(cfg.models_dir),
    )
    result = asr_model.transcribe(audio, batch_size=cfg.batch_size)
    language = result.get("language", cfg.language)
    _free_gpu(asr_model)

    # 2) Alignment -------------------------------------------------------------
    logger.info("[%s] allineamento parole (wav2vec2, %s)", wav.name, language)
    align_model, align_meta = whisperx.load_align_model(
        language_code=language, device=cfg.device, model_dir=str(cfg.models_dir)
    )
    result = whisperx.align(
        result["segments"],
        align_model,
        align_meta,
        audio,
        cfg.device,
        return_char_alignments=cfg.return_char_alignments,
    )
    _free_gpu(align_model)

    # 3) + 4) Diarizzazione e fusione -----------------------------------------
    diarized = False
    if cfg.diarize:
        from whisperx.diarize import DiarizationPipeline

        logger.info("[%s] diarizzazione (%s)", wav.name, cfg.diarization_model)
        diarize_model = DiarizationPipeline(
            model_name=cfg.diarization_model,
            token=hf_token,
            device=cfg.device,
            cache_dir=str(cfg.models_dir),
        )
        diarize_df = diarize_model(
            audio,
            num_speakers=cfg.num_speakers,
            min_speakers=cfg.min_speakers,
            max_speakers=cfg.max_speakers,
        )
        result = whisperx.assign_word_speakers(diarize_df, result)
        diarized = True
        _free_gpu(diarize_model)

    segments = _clean_segments(result.get("segments", []))
    speakers = _count_speakers(segments)

    payload = {
        "source_wav": wav.name,
        "language": language,
        "duration_s": probe_duration_seconds(wav),
        "diarized": diarized,
        "speakers_detected": speakers,
        "num_speakers_detected": len(speakers),
        "whisper_model": cfg.whisper_model,
        "diarization_model": cfg.diarization_model if diarized else None,
        "segments": segments,
    }
    logger.info(
        "[%s] fatto: %d segmenti, %d speaker", wav.name, len(segments), len(speakers)
    )
    return payload


def run(cfg: TranscribeConfig, only_stem: str | None = None) -> list[dict]:
    patch_speechbrain_compat()
    quiet_noisy_logs()
    configure_model_cache(cfg)

    hf_token = load_hf_token() if cfg.diarize else None
    if cfg.diarize and not hf_token:
        raise SystemExit(
            "Diarizzazione attiva ma nessun token HuggingFace trovato.\n"
            "Imposta HF_TOKEN (variabile d'ambiente o nel .env della root del repo) "
            "e accetta le condizioni del modello "
            f"'{cfg.diarization_model}' su huggingface.co.\n"
            "In alternativa imposta 'diarize: false' in config.yaml."
        )

    if only_stem is not None:
        target = cfg.stage0_dir / f"{only_stem}.wav"
        if not target.is_file():
            raise SystemExit(
                f"WAV atteso non trovato: {target}. Esegui prima lo stadio 0 su quel file."
            )
        wavs = [target]
    else:
        wavs = sorted(cfg.stage0_dir.glob("*.wav"))
    if not wavs:
        raise SystemExit(
            f"Nessun WAV in {cfg.stage0_dir}. Esegui prima lo stadio 0 (stage_0_ingest.py)."
        )

    out_dir = cfg.stage1_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = iso_now()
    t_total = time.perf_counter()
    summary: list[dict] = []
    for wav in wavs:
        t_file = time.perf_counter()
        payload = transcribe_one(wav, cfg, hf_token)
        elapsed = time.perf_counter() - t_file
        write_json(out_dir / f"{wav.stem}.json", payload)

        audio_s = payload["duration_s"]
        rtf = (elapsed / audio_s) if audio_s else None
        rtf_txt = f" ({rtf:.2f}x audio)" if rtf else ""
        logger.info("[%s] completato in %s%s", wav.name, format_duration(elapsed), rtf_txt)

        summary.append(
            {
                "file": f"{wav.stem}.json",
                "segments": len(payload["segments"]),
                "speakers": payload["num_speakers_detected"],
                "duration_s": audio_s,
                "elapsed_s": round(elapsed, 1),
            }
        )

    total_elapsed = time.perf_counter() - t_total
    write_json(
        out_dir / "transcribe_log.json",
        {
            "stage_version": STAGE_VERSION,
            "started_at": started_at,
            "finished_at": iso_now(),
            "elapsed_s": round(total_elapsed, 1),
            "device": cfg.device,
            "compute_type": cfg.compute_type,
            "whisper_model": cfg.whisper_model,
            "language": cfg.language,
            "diarize": cfg.diarize,
            "diarization_model": cfg.diarization_model if cfg.diarize else None,
            "files": summary,
        },
    )

    print(
        f"\nStadio 1 completato in {format_duration(total_elapsed)}: "
        f"{len(summary)} file trascritti -> {out_dir}"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stadio 1: trascrizione WhisperX (ASR+align+diarize).")
    parser.add_argument("--config", type=Path, default=None, help="Path a config.yaml")
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Processa solo questo file (nome senza estensione, es. 'barbero'). Default: tutti.",
    )
    args = parser.parse_args()
    run(load_config(args.config), args.only)


if __name__ == "__main__":
    main()
