"""Carica Storie/.env e risolve path relativi alla root del repo."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # src → Adriano_graph → Storie
_ENV_PATH = REPO_ROOT / ".env"


def load_repo_env() -> Path:
    """Carica la root `.env` senza sovrascrivere variabili già in shell."""
    if _ENV_PATH.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(_ENV_PATH, override=False)
        except ImportError:
            for raw in _ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    return _ENV_PATH


def resolve_repo_path(raw: str) -> Path:
    p = Path(raw.strip())
    if p.is_absolute():
        return p
    return (REPO_ROOT / p).resolve()


def default_embed_model() -> str:
    """Id Hugging Face, oppure path (assoluto o relativo alla root del repo)."""
    load_repo_env()
    raw = (os.environ.get("EMBED_MODEL") or "BAAI/bge-m3").strip()
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    candidate = (REPO_ROOT / p).resolve()
    if candidate.exists():
        return str(candidate)
    return raw


def default_embed_device() -> str:
    load_repo_env()
    return (os.environ.get("EMBED_DEVICE") or "cuda").strip() or "cuda"
