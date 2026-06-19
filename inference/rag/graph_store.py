from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_IT_STOP = frozenset({
    "il", "lo", "la", "i", "gli", "le", "un", "una", "di", "a", "da", "in", "con",
    "su", "per", "tra", "fra", "che", "non", "mi", "ti", "si", "ci", "vi",
    "hai", "ho", "ha", "sono", "sei", "era", "stato", "stata", "mai", "piu", "più",
    "quale", "qual", "cosa", "come", "dove", "quando", "perche", "perché", "tu", "io",
    "lui", "lei", "noi", "voi", "loro", "del", "della", "dei", "degli", "delle",
    "al", "allo", "alla", "ai", "agli", "alle", "dal", "dallo", "dalla", "e", "o",
    "ma", "se", "ne", "questo", "questa", "quello", "quella", "the", "una", "uno",
    "gli", "avevi", "avevo", "aveva", "fosse", "sia", "sii", "siete", "siamo",
})


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    name: str
    description: str | None
    source_chunks: frozenset[str]


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    type: str
    role: str | None
    description: str | None


class GraphStore:
    """Grafo risolto in memoria con indici per chunk_id e vicinanza."""

    def __init__(self, graph_path: Path) -> None:
        with graph_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        self._nodes: dict[str, GraphNode] = {}
        self._chunk_to_nodes: dict[str, set[str]] = {}
        self._adjacency: dict[str, list[GraphEdge]] = {}

        for raw in data.get("nodes", []):
            chunk_ids = {
                p["chunk_id"]
                for p in raw.get("provenances", [])
                if p.get("chunk_id")
            }
            node = GraphNode(
                id=raw["id"],
                type=raw["type"],
                name=raw.get("name") or raw["id"],
                description=raw.get("description"),
                source_chunks=frozenset(chunk_ids),
            )
            self._nodes[node.id] = node
            for cid in chunk_ids:
                self._chunk_to_nodes.setdefault(cid, set()).add(node.id)

        for raw in data.get("edges", []):
            edge = GraphEdge(
                source_id=raw["source_id"],
                target_id=raw["target_id"],
                type=raw["type"],
                role=raw.get("role"),
                description=raw.get("description"),
            )
            if edge.source_id not in self._nodes or edge.target_id not in self._nodes:
                continue
            self._adjacency.setdefault(edge.source_id, []).append(edge)
            self._adjacency.setdefault(edge.target_id, []).append(append_reverse(edge))

    def nodes_from_chunks(self, chunk_ids: Iterable[str]) -> set[str]:
        found: set[str] = set()
        for cid in chunk_ids:
            found.update(self._chunk_to_nodes.get(cid, ()))
        return found

    def primary_node_for_chunk(self, chunk_id: str) -> GraphNode | None:
        """Nodo seed principale per un chunk (priorità per tipo, come trim)."""
        candidates = self._chunk_to_nodes.get(chunk_id, set())
        if not candidates:
            return None
        best_id = sorted(
            candidates,
            key=lambda nid: (
                nid in {"adriano"},
                _type_rank(self._nodes[nid].type) if nid in self._nodes else 9,
                nid,
            ),
        )[0]
        return self._nodes.get(best_id)

    def nodes_ranked_by_chunks(
        self, chunks: list
    ) -> list[tuple[str, float, list[str]]]:
        """Nodo → score max tra i chunk recuperati; ordine decrescente per score."""
        best: dict[str, float] = {}
        via: dict[str, list[str]] = {}
        for ch in chunks:
            cid = ch.chunk_id
            for nid in self._chunk_to_nodes.get(cid, set()):
                if ch.score >= best.get(nid, -1.0):
                    best[nid] = ch.score
                via.setdefault(nid, [])
                if cid not in via[nid]:
                    via[nid].append(cid)
        ranked = sorted(best.items(), key=lambda x: (-x[1], x[0]))
        return [(nid, sc, via.get(nid, [])) for nid, sc in ranked]

    def central_node_for_question(self, question: str, chunks: list) -> str | None:
        """
        Nodo seed più centrale rispetto alla domanda: match lessicale su id/nome
        tra i nodi citati dai chunk recuperati (fallback: top per score chunk).
        """
        ranked = self.nodes_ranked_by_chunks(chunks)
        if not ranked:
            return None

        tokens = [
            t
            for t in re.findall(r"[a-z0-9']+", question.lower())
            if len(t) >= 3 and t not in _IT_STOP
        ]
        if not tokens:
            return ranked[0][0]

        best_id: str | None = None
        best_score = -1.0

        for nid, chunk_score, _via in ranked:
            node = self._nodes.get(nid)
            if not node:
                continue
            name_l = node.name.lower()
            nid_l = nid.lower()

            for token in tokens:
                match = 0.0
                if nid_l == token or nid_l.startswith(token + "__"):
                    match = 1000.0
                elif nid_l.endswith("__" + token) or nid_l.endswith("_" + token):
                    match = 900.0
                elif re.search(rf"(^|_){re.escape(token)}(_|$)", nid_l):
                    match = 700.0 - len(nid_l) * 0.01
                elif any(w == token for w in re.split(r"\W+", name_l)):
                    match = 600.0
                elif token in name_l:
                    match = 400.0

                if match <= 0:
                    continue
                if node.type == "Place":
                    match += 50.0

                total = match + chunk_score
                if total > best_score:
                    best_score = total
                    best_id = nid

        return best_id or ranked[0][0]

    def expand_one_hop(
        self,
        seed_ids: set[str],
        max_nodes: int,
    ) -> tuple[set[str], list[GraphEdge]]:
        """Seed + vicini a 1 hop; archi tra nodi nel sotto-graf selezionato."""
        selected = set(seed_ids)
        edges_seen: dict[tuple[str, str, str, str | None], GraphEdge] = {}

        for nid in list(seed_ids):
            for edge in self._adjacency.get(nid, []):
                other = edge.target_id if edge.source_id == nid else edge.source_id
                selected.add(other)
                key = (edge.source_id, edge.target_id, edge.type, edge.role)
                edges_seen[key] = edge

        if len(selected) > max_nodes:
            selected = _trim_nodes(selected, seed_ids, self._nodes, max_nodes)
            edges_seen = {
                k: e
                for k, e in edges_seen.items()
                if e.source_id in selected and e.target_id in selected
            }

        return selected, list(edges_seen.values())

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)


def append_reverse(edge: GraphEdge) -> GraphEdge:
    return GraphEdge(
        source_id=edge.target_id,
        target_id=edge.source_id,
        type=edge.type,
        role=edge.role,
        description=edge.description,
    )


def _trim_nodes(
    candidates: set[str],
    seeds: set[str],
    nodes: dict[str, GraphNode],
    max_nodes: int,
) -> set[str]:
    """Mantiene fino a max_nodes, priorità ai seed poi vicini per tipo."""
    hub_ids = {"adriano"}
    seed_ordered = sorted(
        seeds,
        key=lambda nid: (
            nid in hub_ids,
            _type_rank(nodes[nid].type) if nid in nodes else 9,
            nid,
        ),
    )[:max_nodes]
    if len(seed_ordered) >= max_nodes:
        return set(seed_ordered)

    ordered = list(seed_ordered)
    rest = sorted(
        candidates - set(seed_ordered),
        key=lambda nid: (
            nid in hub_ids,
            _type_rank(nodes[nid].type) if nid in nodes else 9,
            nid,
        ),
    )
    for nid in rest:
        if len(ordered) >= max_nodes:
            break
        ordered.append(nid)
    return set(ordered)


def _type_rank(node_type: str) -> int:
    priority = {
        "Event": 0,
        "Person": 1,
        "Theme": 2,
        "Reflection": 3,
        "Phase": 4,
        "Place": 5,
        "Era": 6,
        "Work": 7,
        "Subject": 8,
    }
    return priority.get(node_type, 9)
