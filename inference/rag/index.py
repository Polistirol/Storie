from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from rag.config import AppConfig
from rag.embedder import Embedder


INDEX_VECTORS = "vectors.npy"
INDEX_META = "meta.json"
INDEX_TEXTS = "chunk_texts.json"


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
    def build(cls, chunks_path: Path, embedder: Embedder) -> "ChunkIndex":
        with chunks_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        records: list[dict] = []
        texts: dict[str, str] = {}
        texts_list: list[str] = []
        for chunk in data["chunks"]:
            part = chunk.get("part") or {}
            cid = chunk["chunk_id"]
            records.append(
                {
                    "chunk_id": cid,
                    "part_number": part.get("number"),
                    "part_title": part.get("title"),
                    "token_count": chunk.get("token_count"),
                }
            )
            texts[cid] = chunk["text"]
            texts_list.append(chunk["text"])

        vectors = embedder.encode(texts_list)
        return cls(vectors, records, embedder.model_name, texts=texts)

    def save(self, index_dir: Path) -> None:
        index_dir.mkdir(parents=True, exist_ok=True)
        np.save(index_dir / INDEX_VECTORS, self.vectors)
        meta = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model_name,
            "num_chunks": len(self.records),
            "records": self.records,
        }
        with (index_dir / INDEX_META).open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with (index_dir / INDEX_TEXTS).open("w", encoding="utf-8") as f:
            json.dump(self._texts, f, ensure_ascii=False)

    @classmethod
    def load(cls, index_dir: Path, *, chunks_path: Path | None = None) -> "ChunkIndex":
        meta_path = index_dir / INDEX_META
        vec_path = index_dir / INDEX_VECTORS
        texts_path = index_dir / INDEX_TEXTS
        if not meta_path.is_file() or not vec_path.is_file():
            raise FileNotFoundError(
                f"Indice non trovato in {index_dir}. Esegui prima build_index.py."
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
                "Testi chunk non caricati. Rilancia build_index.py o load_texts()."
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


def build_and_save(cfg: AppConfig) -> None:
    embedder = Embedder(cfg.embed_model, device=cfg.embed_device)
    index = ChunkIndex.build(cfg.chunks_path, embedder)
    index.save(cfg.index_dir)
    print(f"Indice scritto in {cfg.index_dir} ({len(index.records)} chunk).")
