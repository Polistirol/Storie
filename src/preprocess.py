"""
Prepara file audio per il training: enhancement ClearVoice + normalizzazione loudness.
Path sorgente: un file .wav oppure una cartella (tutti i .wav al primo livello, esclusi *_final.wav):
workspace ClearVoice e *_final.wav restano nella stessa cartella di ogni sorgente.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyloudnorm as pyln
import soundfile as sf
from clearvoice import ClearVoice

# Devono coincidere con model_names e con la sottocartella creata da ClearVoice (process → join(output_path, self.name))
MODEL_SPEECH_ENHANCEMENT = "MossFormer2_SE_48K"
MODEL_SUPER_RESOLUTION = "MossFormer2_SR_48K"


def _wav_sources_in_folder(folder: Path) -> list[Path]:
    """Solo file .wav diretti nella cartella; esclude gli output *_final.wav."""
    folder = folder.resolve()
    out: list[Path] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() != ".wav":
            continue
        if p.stem.endswith("_final"):
            continue
        out.append(p)
    return out


def _cv_written_wav(output_workspace: Path, model_name: str, original_name: str) -> Path:
    """Path effettivo del WAV dopo online_write (workspace / model_name / basename)."""
    return output_workspace / model_name / original_name


def preprocess_audio(file_path: Path) -> Path:
    file_path = file_path.resolve()
    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    parent = file_path.parent
    stem = file_path.stem
    name = file_path.name

    # Cartelle dedicate (non suffissare .wav: ClearVoice tratta output_path come directory base)
    se_workspace = parent / f"{stem}_cv_se"
    sr_workspace = parent / f"{stem}_cv_sr"

    cv_se = ClearVoice(task="speech_enhancement", model_names=[MODEL_SPEECH_ENHANCEMENT])
    cv_se(input_path=str(file_path), online_write=True, output_path=str(se_workspace))

    clean_wav = _cv_written_wav(se_workspace, MODEL_SPEECH_ENHANCEMENT, name)
    if not clean_wav.is_file():
        raise FileNotFoundError(
            f"Enhancement non trovato (atteso): {clean_wav}"
        )

    cv_sr = ClearVoice(task="speech_super_resolution", model_names=[MODEL_SUPER_RESOLUTION])
    cv_sr(input_path=str(clean_wav), online_write=True, output_path=str(sr_workspace))

    enhanced_wav = _cv_written_wav(sr_workspace, MODEL_SUPER_RESOLUTION, name)
    if not enhanced_wav.is_file():
        raise FileNotFoundError(
            f"Super-resolution non trovato (atteso): {enhanced_wav}"
        )

    data, sr = sf.read(str(enhanced_wav))
    meter = pyln.Meter(sr)
    input_lufs = meter.integrated_loudness(data)
    normalized = pyln.normalize.loudness(data, input_lufs, -18.0)

    final_path = parent / f"{stem}_final.wav"
    sf.write(str(final_path), normalized, sr)
    return final_path


def preprocess_folder(folder: Path) -> list[Path]:
    folder = folder.resolve()
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    sources = _wav_sources_in_folder(folder)
    if not sources:
        raise FileNotFoundError(f"Nessun .wav da processare in {folder}")
    return [preprocess_audio(p) for p in sources]


if __name__ == "__main__":
    default = Path(r"resources\audio_seed\nonno\train")
    src = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else default
    src = src.resolve()
    if src.is_dir():
        outs = preprocess_folder(src)
        print(f"Processati {len(outs)} file in {src}:")
        for o in outs:
            print(f"  {o}")
    else:
        out = preprocess_audio(src)
        print(f"Scritto: {out}")
