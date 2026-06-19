from __future__ import annotations

from typing import Optional

import numpy as np


class Embedder:
    """Wrapper lazy su sentence-transformers (BGE-M3)."""

    def __init__(self, model_name: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise SystemExit(
                "sentence-transformers non installato. "
                "Da inference/: pip install -r requirements.txt"
            ) from exc
        self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def encode(self, texts: list[str], batch_size: int = 8) -> np.ndarray:
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=len(texts) > 16,
        )
        return np.asarray(vectors, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        model = self._load()
        vec = model.encode(
            text,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        arr = np.asarray(vec, dtype=np.float32)
        return arr.reshape(-1) if arr.ndim > 1 else arr
