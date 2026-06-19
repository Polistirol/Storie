from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

_INFERENCE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _INFERENCE_ROOT / "config.yaml"


@dataclass(frozen=True)
class AppConfig:
    chunks_path: Path
    graph_path: Path
    index_dir: Path
    embed_model: str
    embed_device: str
    top_k_chunks: int
    max_graph_nodes: int
    max_description_chars: int
    max_chunk_chars: int
    lmstudio_url: str
    lmstudio_model: Optional[str]
    temperature: float
    max_tokens: int
    disable_thinking: bool


def _resolve(base: Path, raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def load_config(path: Path | str | None = None) -> AppConfig:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    with cfg_path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    root = cfg_path.resolve().parent
    return AppConfig(
        chunks_path=_resolve(root, raw["chunks_path"]),
        graph_path=_resolve(root, raw["graph_path"]),
        index_dir=_resolve(root, raw.get("index_dir", "data/index")),
        embed_model=str(raw.get("embed_model", "BAAI/bge-m3")),
        embed_device=str(raw.get("embed_device", "cpu")),
        top_k_chunks=int(raw.get("top_k_chunks", 5)),
        max_graph_nodes=int(raw.get("max_graph_nodes", 25)),
        max_description_chars=int(raw.get("max_description_chars", 280)),
        max_chunk_chars=int(raw.get("max_chunk_chars", 0)),
        lmstudio_url=str(raw.get("lmstudio_url", "http://localhost:1234/v1")),
        lmstudio_model=raw.get("lmstudio_model"),
        temperature=float(raw.get("temperature", 0.3)),
        max_tokens=int(raw.get("max_tokens", 512)),
        disable_thinking=bool(raw.get("disable_thinking", True)),
    )
