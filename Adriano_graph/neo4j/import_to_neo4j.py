"""
import_to_neo4j.py
==================
Importa extracted_graphs.json (lista di ExtractedGraph) in un'istanza
Neo4j locale. Usa MERGE su ID per idempotenza: eseguibile più volte
senza duplicare nodi/archi.

Dipendenze:
    pip install neo4j pydantic

Uso:
    python import_to_neo4j.py --file extracted_graphs.json

Opzioni:
    --uri       URI Neo4j  (default: bolt://localhost:7687)
    --user      utente     (default: neo4j)
    --password  password   (default: password)
    --file      path JSON  (default: extracted_graphs.json)
    --dry-run   valida JSON senza toccare Neo4j
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from neo4j import GraphDatabase
from pydantic import ValidationError

# ── aggiunge src/ al path se lo script è nella root del progetto ──────────────
sys.path.insert(0, str(Path(__file__).parent / "src"))
from schema import ExtractedGraph, is_edge_valid, NodeType  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Cypher helpers
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_node_cypher(node_type: str) -> str:
    """
    MERGE su id, poi SET di tutti i campi.
    Il label Neo4j coincide con NodeType (Person, Event, ecc.).
    Aggiunge sempre il label generico :KGNode per query cross-tipo.
    """
    return f"""
    MERGE (n:{node_type}:KGNode {{id: $id}})
    SET
        n.name            = $name,
        n.description     = $description,
        n.aliases         = $aliases,
        n.chunk_id        = $chunk_id,
        n.model           = $model,
        n.timestamp       = $timestamp,
        n.schema_version  = $schema_version,
        n.confidence      = $confidence,
        n.evidence_span   = $evidence_span,
        n.human_validated = $human_validated
    """


_UPSERT_EDGE_CYPHER = """
    MATCH (src:KGNode {id: $source_id})
    MATCH (tgt:KGNode {id: $target_id})
    MERGE (src)-[r:{edge_type} {{chunk_id: $chunk_id}}]->(tgt)
    SET
        r.description     = $description,
        r.model           = $model,
        r.timestamp       = $timestamp,
        r.schema_version  = $schema_version,
        r.confidence      = $confidence,
        r.evidence_span   = $evidence_span,
        r.human_validated = $human_validated
"""


# ─────────────────────────────────────────────────────────────────────────────
# Import
# ─────────────────────────────────────────────────────────────────────────────

def import_graphs(
    graphs: list[ExtractedGraph],
    driver,
    *,
    dry_run: bool = False,
) -> dict:
    stats = {"nodes_upserted": 0, "edges_upserted": 0,
             "edges_skipped_invalid": 0, "edges_skipped_missing_node": 0}

    # Indice id -> type per validazione archi
    node_type_map: dict[str, NodeType] = {}
    for g in graphs:
        for n in g.nodes:
            node_type_map[n.id] = n.type

    if dry_run:
        log.info("[DRY RUN] Validazione completata. Nodi distinti: %d", len(node_type_map))
        total_edges = sum(len(g.edges) for g in graphs)
        log.info("[DRY RUN] Archi totali (pre-validazione): %d", total_edges)
        return stats

    with driver.session() as session:
        # ── Crea constraint di unicità (idempotente) ─────────────────────────
        for node_type in NodeType:
            session.run(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{node_type.value}) "
                f"REQUIRE n.id IS UNIQUE"
            )
        log.info("Constraint di unicità creati/verificati.")

        # ── Nodi ─────────────────────────────────────────────────────────────
        for g in graphs:
            for node in g.nodes:
                p = node.provenance
                session.run(
                    _upsert_node_cypher(node.type.value),
                    id=node.id,
                    name=node.name,
                    description=node.description,
                    aliases=node.aliases,
                    chunk_id=p.chunk_id,
                    model=p.model,
                    timestamp=p.timestamp.isoformat(),
                    schema_version=p.schema_version,
                    confidence=p.confidence,
                    evidence_span=p.evidence_span,
                    human_validated=p.human_validated,
                )
                stats["nodes_upserted"] += 1

        log.info("Nodi importati: %d", stats["nodes_upserted"])

        # ── Archi ─────────────────────────────────────────────────────────────
        for g in graphs:
            for edge in g.edges:
                # Controlla che entrambi i nodi esistano nel map
                if edge.source_id not in node_type_map or edge.target_id not in node_type_map:
                    log.warning(
                        "Arco %s->%s ignorato: nodo mancante",
                        edge.source_id, edge.target_id
                    )
                    stats["edges_skipped_missing_node"] += 1
                    continue

                # Validazione strutturale dallo schema
                if not is_edge_valid(
                    edge.type,
                    node_type_map[edge.source_id],
                    node_type_map[edge.target_id],
                ):
                    log.warning(
                        "Arco %s -[%s]-> %s ignorato: violazione EDGE_COMPATIBILITY",
                        edge.source_id, edge.type.value, edge.target_id
                    )
                    stats["edges_skipped_invalid"] += 1
                    continue

                p = edge.provenance
                # Il tipo di relazione in Cypher deve essere nel MERGE,
                # non può essere parametrizzato → f-string (sicuro: viene da un Enum)
                cypher = _UPSERT_EDGE_CYPHER.format(edge_type=edge.type.value)
                session.run(
                    cypher,
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    chunk_id=p.chunk_id,
                    description=edge.description,
                    model=p.model,
                    timestamp=p.timestamp.isoformat(),
                    schema_version=p.schema_version,
                    confidence=p.confidence,
                    evidence_span=p.evidence_span,
                    human_validated=p.human_validated,
                )
                stats["edges_upserted"] += 1

        log.info("Archi importati: %d  |  saltati (invalid): %d  |  saltati (nodo mancante): %d",
                 stats["edges_upserted"],
                 stats["edges_skipped_invalid"],
                 stats["edges_skipped_missing_node"])

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    repo_root = Path(__file__).resolve().parent.parent.parent
    env_path = repo_root / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Importa extracted_graphs.json in Neo4j")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", "password"))
    parser.add_argument("--file",     default="extracted_graphs.json")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Valida il JSON senza scrivere su Neo4j")
    args = parser.parse_args()

    # ── Carica e valida JSON ──────────────────────────────────────────────────
    path = Path(args.file)
    if not path.exists():
        log.error("File non trovato: %s", path)
        sys.exit(1)

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        log.error("Il JSON deve essere una lista di ExtractedGraph")
        sys.exit(1)

    graphs: list[ExtractedGraph] = []
    errors = 0
    for i, item in enumerate(raw):
        try:
            graphs.append(ExtractedGraph.model_validate(item))
        except ValidationError as e:
            log.error("Chunk %d: errore di validazione → %s", i, e)
            errors += 1

    if errors:
        log.warning("%d chunk con errori di validazione (ignorati)", errors)

    log.info("Chunk validi: %d / %d", len(graphs), len(raw))
    log.info("Nodi totali (con duplicati cross-chunk): %d",
             sum(len(g.nodes) for g in graphs))
    log.info("Archi totali: %d", sum(len(g.edges) for g in graphs))

    # ── Connessione e import ──────────────────────────────────────────────────
    if args.dry_run:
        import_graphs(graphs, driver=None, dry_run=True)
        return

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        driver.verify_connectivity()
        log.info("Connesso a Neo4j: %s", args.uri)
        stats = import_graphs(graphs, driver)
        log.info("Import completato: %s", stats)
    finally:
        driver.close()


if __name__ == "__main__":
    main()