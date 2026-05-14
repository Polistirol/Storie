# src/schema.py
"""
Schema del knowledge graph biografico.

Definisce i tipi di nodo, i tipi di arco, le regole di compatibilità,
e i modelli Pydantic per validare l'output dell'estrattore (stadio 3).

Modulo puro strutturale: NIENTE testo di prompt, NIENTE istruzioni di
estrazione, NIENTE riferimenti al dominio specifico (Yourcenar, clinico,
ecc.). Modifiche qui = bump di SCHEMA_VERSION = ri-estrazione dei chunk
perché cambia la shape dei dati.

Il "contratto semantico" verso il modello (descrizione dei tipi di nodo
e arco in italiano, distinzione Event/Reflection, regole operative,
esempi few-shot) vive in `src/stage_3_prompt.py` e ha un suo
PROMPT_VERSION indipendente. Vedi ADR-012.

Principio: schema descrittivo, non tipato per tipo di nodo.
Tutti i nodi condividono la stessa shape (id, type, name, description, ...).
Le specificità di dominio vivono nel prompt, non nei campi Pydantic.
Se in futuro un campo diventa stabile e ricorrente, lo si promuove qui.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "0.1.0"


# -----------------------------------------------------------------------------
# Tipi di nodo
# -----------------------------------------------------------------------------

class NodeType(str, Enum):
    PERSON = "Person"
    EVENT = "Event"
    PLACE = "Place"
    PHASE = "Phase"
    THEME = "Theme"
    REFLECTION = "Reflection"
    WORK = "Work"


# -----------------------------------------------------------------------------
# Tipi di arco
# -----------------------------------------------------------------------------

class EdgeType(str, Enum):
    # Fattuali
    INVOLVES = "INVOLVES"           # Event -> Person
    LOCATED_AT = "LOCATED_AT"       # Event -> Place
    DURING = "DURING"               # Event -> Phase
    CREATED = "CREATED"             # Person -> Work
    RELATED_TO = "RELATED_TO"       # generico, ultima spiaggia

    # Tematiche / riflessive
    EMBODIES = "EMBODIES"           # Event/Person -> Theme
    REFLECTS_ON = "REFLECTS_ON"     # Reflection -> qualsiasi cosa
    ECHOES = "ECHOES"               # Event -> Event (eco narrativo)
    CONTRASTS_WITH = "CONTRASTS_WITH"  # Event -> Event, Theme -> Theme
    TRANSFORMS_INTO = "TRANSFORMS_INTO"  # Phase -> Phase, Person -> Person (cambiamento interiore)

    # Causali / temporali
    CAUSED = "CAUSED"               # Event -> Event
    FOLLOWS = "FOLLOWS"             # Event -> Event (successione temporale)


# -----------------------------------------------------------------------------
# Compatibilità arco -> (tipi sorgente ammessi, tipi destinazione ammessi)
#
# Serve a due cose:
# 1) validare a posteriori l'output dell'estrattore (filtrare archi assurdi).
# 2) documentare il modello in modo che il prompt possa elencare le combinazioni
#    legittime senza ambiguità.
#
# Insiemi vuoti = qualsiasi tipo ammesso. Da usare con parsimonia.
# -----------------------------------------------------------------------------

EDGE_COMPATIBILITY: dict[EdgeType, tuple[set[NodeType], set[NodeType]]] = {
    EdgeType.INVOLVES:        ({NodeType.EVENT}, {NodeType.PERSON}),
    EdgeType.LOCATED_AT:      ({NodeType.EVENT}, {NodeType.PLACE}),
    EdgeType.DURING:          ({NodeType.EVENT}, {NodeType.PHASE}),
    EdgeType.CREATED:         ({NodeType.PERSON}, {NodeType.WORK}),
    EdgeType.RELATED_TO:      (set(), set()),  # jolly: usabile, ma il prompt lo scoraggia
    EdgeType.EMBODIES:        ({NodeType.EVENT, NodeType.PERSON}, {NodeType.THEME}),
    EdgeType.REFLECTS_ON:     ({NodeType.REFLECTION}, set()),  # può riflettere su qualsiasi cosa
    EdgeType.ECHOES:          ({NodeType.EVENT}, {NodeType.EVENT}),
    EdgeType.CONTRASTS_WITH:  ({NodeType.EVENT, NodeType.THEME}, {NodeType.EVENT, NodeType.THEME}),
    EdgeType.TRANSFORMS_INTO: ({NodeType.PHASE, NodeType.PERSON}, {NodeType.PHASE, NodeType.PERSON}),
    EdgeType.CAUSED:          ({NodeType.EVENT}, {NodeType.EVENT}),
    EdgeType.FOLLOWS:         ({NodeType.EVENT}, {NodeType.EVENT}),
}


# -----------------------------------------------------------------------------
# Modelli Pydantic
#
# Shape unica per tutti i nodi. type discrimina la semantica.
# I campi opzionali esistono perché l'estrattore può lasciarli vuoti senza
# rompere il parsing.
# -----------------------------------------------------------------------------

class Provenance(BaseModel):
    """Provenienza condivisa da nodi e archi. Vale anche dopo il merge in stadio 4."""
    chunk_id: str                      # da chunks.json
    model: str                         # es. "claude-sonnet-4-6-20260101"
    timestamp: datetime
    schema_version: str = SCHEMA_VERSION
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    evidence_span: Optional[str] = None  # citazione testuale (substring del chunk) su cui il modello si è basato
    human_validated: bool = False


class Node(BaseModel):
    id: str                            # canonico, deterministico (vedi nota in fondo)
    type: NodeType
    name: str                          # forma italiana canonica: "Antinoo", "Traiano", "Villa Adriana"
    description: Optional[str] = None  # breve sintesi del modello, 1-2 frasi
    aliases: list[str] = Field(default_factory=list)  # varianti incontrate nel testo
    provenance: Provenance

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name vuoto")
        return v.strip()


class Edge(BaseModel):
    source_id: str
    target_id: str
    type: EdgeType
    description: Optional[str] = None  # opzionale: glossa testuale del motivo dell'arco
    provenance: Provenance


class ExtractedGraph(BaseModel):
    """Output di una singola chiamata extract_from_chunk()."""
    chunk_id: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Validazione strutturale
# -----------------------------------------------------------------------------

def is_edge_valid(edge_type: EdgeType, source_type: NodeType, target_type: NodeType) -> bool:
    """
    Validazione post-estrazione. Restituisce False per archi che violano
    EDGE_COMPATIBILITY. Insiemi vuoti = jolly ammesso.
    Da usare in stadio 3 dopo il parsing per filtrare/loggare archi assurdi
    senza far esplodere la chiamata.
    """
    allowed_src, allowed_tgt = EDGE_COMPATIBILITY[edge_type]
    if allowed_src and source_type not in allowed_src:
        return False
    if allowed_tgt and target_type not in allowed_tgt:
        return False
    return True