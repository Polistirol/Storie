"""Caricamento della configurazione della pipeline di trascrizione.

Legge transcribe/config.yaml e lo espone come dataclass immutabile.
I path relativi nel YAML sono risolti rispetto alla cartella transcribe/.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

SRC_DIR = Path(__file__).resolve().parent
TRANSCRIBE_ROOT = SRC_DIR.parent
REPO_ROOT = TRANSCRIBE_ROOT.parent
_DEFAULT_CONFIG = TRANSCRIBE_ROOT / "config.yaml"


@dataclass(frozen=True)
class TranscribeConfig:
    # I/O
    input_dir: Path
    data_dir: Path
    models_dir: Path
    input_extensions: tuple[str, ...]
    # device / compute
    device: str
    compute_type: str
    # stadio 0
    target_sample_rate: int
    target_channels: int
    loudnorm: bool
    # stadio 1 - ASR
    whisper_model: str
    language: str
    batch_size: int
    return_char_alignments: bool
    # stadio 1 - diarizzazione
    diarize: bool
    diarization_model: str
    num_speakers: Optional[int]
    min_speakers: Optional[int]
    max_speakers: Optional[int]
    # stadio 1b - rifinitura speaker (embedding)
    refine_speakers: bool
    embedding_model: str
    enrollment_dir: Path
    refine_min_segment_s: float
    refine_margin: float
    split_long_segments: bool
    refine_window_s: float
    # stadio 2
    speaker_labels: tuple[str, ...]
    include_timestamps: bool
    # stadio 3
    recap_prompt_file: Path
    lm_studio_url: str
    lm_studio_model: Optional[str]
    recap_temperature: float
    recap_max_tokens: int

    @property
    def stage0_dir(self) -> Path:
        return self.data_dir / "stage_0_ingest"

    @property
    def stage1_dir(self) -> Path:
        return self.data_dir / "stage_1_transcribe"

    @property
    def stage1b_dir(self) -> Path:
        return self.data_dir / "stage_1b_refine"

    @property
    def stage2_dir(self) -> Path:
        return self.data_dir / "stage_2_dialogue"

    @property
    def stage3_dir(self) -> Path:
        return self.data_dir / "stage_3_recap"

    @property
    def stage2_source_dir(self) -> Path:
        """Cartella da cui lo stadio 2 legge i JSON (rifiniti se la rifinitura e' attiva)."""
        return self.stage1b_dir if self.refine_speakers else self.stage1_dir


def _resolve(base: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (base / p).resolve()


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _load_repo_env() -> None:
    """Carica Storie/.env senza sovrascrivere variabili già in shell."""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:
        pass
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: Path | str | None = None) -> TranscribeConfig:
    _load_repo_env()
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    with cfg_path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    root = cfg_path.resolve().parent
    extensions = tuple(
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in raw.get("input_extensions", [".mp3", ".wav", ".m4a", ".flac"])
    )
    return TranscribeConfig(
        input_dir=_resolve(root, raw.get("input_dir", "resources/raw_audio")),
        data_dir=_resolve(root, raw.get("data_dir", "data")),
        models_dir=_resolve(root, raw.get("models_dir", "models")),
        input_extensions=extensions,
        device=str(raw.get("device", "cuda")),
        compute_type=str(raw.get("compute_type", "float16")),
        target_sample_rate=int(raw.get("target_sample_rate", 16000)),
        target_channels=int(raw.get("target_channels", 1)),
        loudnorm=bool(raw.get("loudnorm", True)),
        whisper_model=str(raw.get("whisper_model", "large-v3")),
        language=str(raw.get("language", "it")),
        batch_size=int(raw.get("batch_size", 8)),
        return_char_alignments=bool(raw.get("return_char_alignments", False)),
        diarize=bool(raw.get("diarize", True)),
        diarization_model=str(
            raw.get("diarization_model", "pyannote/speaker-diarization-community-1")
        ),
        num_speakers=_opt_int(raw.get("num_speakers")),
        min_speakers=_opt_int(raw.get("min_speakers")),
        max_speakers=_opt_int(raw.get("max_speakers")),
        refine_speakers=bool(raw.get("refine_speakers", False)),
        embedding_model=str(raw.get("embedding_model", "speechbrain/spkrec-ecapa-voxceleb")),
        enrollment_dir=_resolve(root, raw.get("enrollment_dir", "resources/speakers")),
        refine_min_segment_s=float(raw.get("refine_min_segment_s", 0.7)),
        refine_margin=float(raw.get("refine_margin", 0.10)),
        split_long_segments=bool(raw.get("split_long_segments", True)),
        refine_window_s=float(raw.get("refine_window_s", 1.0)),
        speaker_labels=tuple(str(x) for x in raw.get("speaker_labels", ["A", "B"])),
        include_timestamps=bool(raw.get("include_timestamps", True)),
        recap_prompt_file=_resolve(
            root, raw.get("recap_prompt_file", "resources/recap_instruction_prompt.md")
        ),
        lm_studio_url=str(raw.get("lm_studio_url", "http://localhost:1234/v1")),
        lm_studio_model=raw.get("lm_studio_model"),
        recap_temperature=float(raw.get("recap_temperature", 0.3)),
        recap_max_tokens=int(raw.get("recap_max_tokens", 2048)),
    )
