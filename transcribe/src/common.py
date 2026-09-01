"""Utility condivise tra gli stadi della pipeline di trascrizione.

Logging, serializzazione JSON stabile, helper ffmpeg/ffprobe, lettura del token
HuggingFace e scoperta dei file audio in input.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from config import REPO_ROOT, TranscribeConfig

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def patch_speechbrain_compat() -> None:
    """Evita il crash di pyannote quando SpeechBrain e' installato su Windows.

    SpeechBrain espone integrazioni opzionali (k2, nlp, ...) come moduli "lazy" che
    importano dipendenze spesso assenti. Ha una guardia per non importarli quando e'
    inspect.py a toccarli, ma il controllo usa il suffisso POSIX '/inspect.py' e su
    Windows ('\\inspect.py') fallisce: lightning, caricando il checkpoint pyannote,
    chiama inspect.stack() -> hasattr(modulo, '__file__') -> import lazy -> crash.

    Diamo a LazyModule un __file__ statico: hasattr(modulo, '__file__') ha successo
    senza innescare l'import lazy, mentre l'accesso a classi reali (es. EncoderClassifier)
    continua a funzionare tramite __getattr__.
    """
    try:
        from speechbrain.utils import importutils as _iu
    except Exception:
        return
    lazy_cls = getattr(_iu, "LazyModule", None)
    if lazy_cls is not None and getattr(lazy_cls, "__file__", None) is None:
        try:
            lazy_cls.__file__ = "<speechbrain-lazy>"
        except Exception:
            pass


def quiet_noisy_logs() -> None:
    """Abbassa il rumore di log/warning di dipendenze molto verbose (benigni)."""
    for name in (
        "speechbrain",
        "pyannote",
        "pytorch_lightning",
        "lightning",
        "lightning.pytorch",
        "lightning.pytorch.utilities.migration.utils",
        "whisperx.vads.pyannote",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    for pattern in (
        ".*torchcodec.*",
        ".*list_audio_backends.*",
        ".*TensorFloat-32.*",
        ".*upgraded your loaded checkpoint.*",
        ".*does not support them in.*",  # symlink cache HF
    ):
        warnings.filterwarnings("ignore", message=pattern)


def iso_now() -> str:
    """Timestamp UTC in formato ISO-8601 (...Z), senza microsecondi."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def json_dumps_stable(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps_stable(obj), encoding="utf-8", newline="\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def format_timestamp(seconds: float) -> str:
    """Secondi -> 'mm:ss' (o 'h:mm:ss' oltre l'ora)."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_duration(seconds: float) -> str:
    """Durata leggibile per i timer: '12.3s', '4m 05s', '1h 02m 03s'."""
    seconds = max(0.0, seconds)
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s"


# -----------------------------------------------------------------------------
# ffmpeg / ffprobe
# -----------------------------------------------------------------------------


def require_tool(name: str) -> str:
    """Verifica che un eseguibile (ffmpeg/ffprobe) sia nel PATH dell'env."""
    exe = shutil.which(name)
    if exe is None:
        raise SystemExit(
            f"'{name}' non trovato nel PATH. Attiva l'env: "
            f"micromamba activate adriano-transcribe"
        )
    return exe


def run_command(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def probe_duration_seconds(path: Path) -> Optional[float]:
    """Durata in secondi via ffprobe; None se non determinabile."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    proc = run_command(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    out = (proc.stdout or "").strip()
    try:
        return float(out)
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Scoperta file audio
# -----------------------------------------------------------------------------


def discover_audio_files(
    input_dir: Path, extensions: Iterable[str]
) -> list[Path]:
    """Audio nella cartella di input con estensione accettata (match case-insensitive)."""
    exts = {e.lower() for e in extensions}
    if not input_dir.is_dir():
        return []
    files = [
        p
        for p in sorted(input_dir.iterdir())
        if p.is_file() and p.suffix.lower() in exts
    ]
    return files


# -----------------------------------------------------------------------------
# Token HuggingFace (per la diarizzazione pyannote)
# -----------------------------------------------------------------------------

_HF_TOKEN_KEYS = ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def load_hf_token() -> Optional[str]:
    """Token HF da variabile d'ambiente; fallback: parsing del .env nella root del repo."""
    for key in _HF_TOKEN_KEYS:
        value = os.environ.get(key)
        if value:
            return value.strip()

    env_path = REPO_ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in _HF_TOKEN_KEYS:
                return value.strip().strip('"').strip("'")
    return None


def configure_model_cache(cfg: TranscribeConfig) -> None:
    """Dirotta le cache dei pesi (HF + torch hub) nella cartella models_dir del progetto.

    Va chiamata PRIMA di importare whisperx/torch perche' alcune librerie leggono
    queste variabili al momento dell'import.
    """
    cfg.models_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cfg.models_dir))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(cfg.models_dir / "hub"))
    os.environ.setdefault("TORCH_HOME", str(cfg.models_dir / "torch"))
    # Windows senza Developer Mode/admin non puo' creare i symlink della cache HF
    # (OSError WinError 1314): disabilitiamo i symlink (i file vengono duplicati).
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
