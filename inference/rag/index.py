"""Caricamento e ricerca sull'indice vettoriale.

La COSTRUZIONE dell'indice NON vive più qui: è lo Stadio 6 della pipeline
(`Adriano_graph/src/stage_6-1_index.py`) a produrre `vectors.npy`, `meta.json`,
`chunk_texts.json` e `manifest.json`. L'inferenza si limita a caricarli e a
interrogarli (ADR-029). Il contratto di formato è documentato nel manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


INDEX_VECTORS = "vectors.npy"
INDEX_META = "meta.json"
INDEX_TEXTS = "chunk_texts.json"
INDEX_MANIFEST = "manifest.json"


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    part_title: str
    part_number: int
    text: str
    score: float = 0.0


class ChunkIndex:
    """Indice vettoriale sui chunk (cosine via dot product, vettori normalizzati)."""

    def __init__(
        self,
        vectors: np.ndarray,
        records: list[dict],
        model_name: str,
        texts: dict[str, str] | None = None,
    ) -> None:
        self.vectors = vectors
        self.records = records
        self.model_name = model_name
        self._texts: dict[str, str] = texts or {}

    def has_texts(self) -> bool:
        return bool(self._texts)

    def load_texts(self, chunks_path: Path) -> None:
        """Carica testi chunk una tantum (fallback se chunk_texts.json assente)."""
        if self._texts:
            return
        self._texts = _load_chunk_texts(chunks_path)

    @classmethod
    def load(cls, index_dir: Path, *, chunks_path: Path | None = None) -> "ChunkIndex":
        meta_path = index_dir / INDEX_META
        vec_path = index_dir / INDEX_VECTORS
        texts_path = index_dir / INDEX_TEXTS
        if not meta_path.is_file() or not vec_path.is_file():
            raise FileNotFoundError(
                f"Indice non trovato in {index_dir}. Costruiscilo con lo Stadio 6: "
                f"da Adriano_graph/ esegui `python src/stage_6-1_index.py`."
            )
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
        vectors = np.load(vec_path)
        texts: dict[str, str] = {}
        if texts_path.is_file():
            with texts_path.open("r", encoding="utf-8") as f:
                texts = json.load(f)
        index = cls(vectors, meta["records"], meta["model"], texts=texts)
        if not index.has_texts() and chunks_path is not None:
            index.load_texts(chunks_path)
        return index

    def search(self, query_vector: np.ndarray, top_k: int) -> list[ChunkRecord]:
        if not self._texts:
            raise RuntimeError(
                "Testi chunk non caricati. Rigenera lo Stadio 6 (stage_6-1_index.py) "
                "o chiama load_texts()."
            )
        scores = self.vectors @ query_vector
        k = min(top_k, len(scores))
        top_rows = np.argpartition(-scores, k - 1)[:k]
        top_rows = top_rows[np.argsort(-scores[top_rows])]

        out: list[ChunkRecord] = []
        for row in top_rows:
            rec = self.records[int(row)]
            cid = rec["chunk_id"]
            out.append(
                ChunkRecord(
                    chunk_id=cid,
                    part_number=int(rec.get("part_number") or 0),
                    part_title=str(rec.get("part_title") or ""),
                    text=self._texts[cid],
                    score=float(scores[int(row)]),
                )
            )
        return out


def _load_chunk_texts(chunks_path: Path) -> dict[str, str]:
    with chunks_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {c["chunk_id"]: c["text"] for c in data["chunks"]}


def load_manifest(index_dir: Path) -> dict | None:
    """Manifest di provenienza scritto dallo Stadio 6 (None se assente)."""
    manifest_path = index_dir / INDEX_MANIFEST
    if not manifest_path.is_file():
        return None
    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
