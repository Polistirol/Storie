from __future__ import annotations

from dataclasses import dataclass, field

from rag.config import AppConfig
from rag.embedder import Embedder
from rag.graph_store import GraphEdge, GraphStore
from rag.index import ChunkIndex, ChunkRecord


@dataclass
class RetrievalTimings:
    embed_s: float = 0.0
    search_s: float = 0.0
    graph_s: float = 0.0
    format_s: float = 0.0

    @property
    def total_s(self) -> float:
        return self.embed_s + self.search_s + self.graph_s + self.format_s


@dataclass
class RetrievalResult:
    question: str
    chunks: list[ChunkRecord] = field(default_factory=list)
    seed_node_ids: set[str] = field(default_factory=set)
    graph_node_ids: set[str] = field(default_factory=set)
    graph_edges: list[GraphEdge] = field(default_factory=list)
    central_node_id: str | None = None
    timings: RetrievalTimings | None = None


class Retriever:
    def __init__(
        self,
        cfg: AppConfig,
        index: ChunkIndex,
        graph: GraphStore,
        embedder: Embedder,
    ) -> None:
        self.cfg = cfg
        self.index = index
        self.graph = graph
        self.embedder = embedder

    def retrieve(
        self,
        question: str,
        *,
        top_k: int | None = None,
        central_question: str | None = None,
    ) -> RetrievalResult:
        import time

        k = top_k if top_k is not None else self.cfg.top_k_chunks
        t0 = time.perf_counter()
        q_vec = self.embedder.encode_one(question)
        t1 = time.perf_counter()
        chunks = self.index.search(q_vec, top_k=k)
        t2 = time.perf_counter()
        chunk_ids = {c.chunk_id for c in chunks}
        seeds = self.graph.nodes_from_chunks(chunk_ids)
        node_ids, edges = self.graph.expand_one_hop(
            seeds,
            max_nodes=self.cfg.max_graph_nodes,
        )
        t3 = time.perf_counter()
        central_q = central_question if central_question is not None else question
        central_id = self.graph.central_node_for_question(central_q, chunks)
        timings = RetrievalTimings(
            embed_s=t1 - t0,
            search_s=t2 - t1,
            graph_s=t3 - t2,
        )
        return RetrievalResult(
            question=question,
            chunks=chunks,
            seed_node_ids=seeds,
            graph_node_ids=node_ids,
            graph_edges=edges,
            central_node_id=central_id,
            timings=timings,
        )


def format_context(
    result: RetrievalResult,
    graph: GraphStore,
    max_description_chars: int,
    *,
    max_chunk_chars: int = 0,
) -> str:
    import time

    t0 = time.perf_counter()
    lines: list[str] = []

    lines.append("=== PASSAGGI DEL TESTO SORGENTE ===")
    for i, ch in enumerate(result.chunks, 1):
        body = _truncate(ch.text.strip(), max_chunk_chars)
        lines.append(
            f"\n[{i}] chunk {ch.chunk_id} | parte {ch.part_number}: {ch.part_title} "
            f"(score {ch.score:.3f})"
        )
        lines.append(body)

    lines.append("\n=== ENTITÀ E RELAZIONI (grafo, 1 hop) ===")
    for nid in sorted(result.graph_node_ids):
        node = graph.get_node(nid)
        if not node:
            continue
        desc = _truncate(node.description, max_description_chars)
        chunks_hint = ", ".join(sorted(node.source_chunks)[:4])
        if len(node.source_chunks) > 4:
            chunks_hint += ", ..."
        lines.append(
            f"- {node.type} «{node.name}» (id={node.id})"
            + (f": {desc}" if desc else "")
            + (f" [chunk: {chunks_hint}]" if chunks_hint else "")
        )

    if result.graph_edges:
        lines.append("\nRelazioni:")
        for edge in _sorted_edges(result.graph_edges, graph):
            role = f", ruolo={edge.role}" if edge.role else ""
            desc = _truncate(edge.description, 120)
            tail = f" — {desc}" if desc else ""
            src = graph.get_node(edge.source_id)
            tgt = graph.get_node(edge.target_id)
            src_name = src.name if src else edge.source_id
            tgt_name = tgt.name if tgt else edge.target_id
            lines.append(
                f"- {src_name} --[{edge.type}{role}]--> {tgt_name}{tail}"
            )

    if result.timings is not None:
        result.timings.format_s = time.perf_counter() - t0
    return "\n".join(lines)


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sorted_edges(edges: list[GraphEdge], graph: GraphStore) -> list[GraphEdge]:
    def key(e: GraphEdge) -> tuple:
        src = graph.get_node(e.source_id)
        tgt = graph.get_node(e.target_id)
        return (e.type, src.name if src else e.source_id, tgt.name if tgt else e.target_id)

    return sorted(edges, key=key)
