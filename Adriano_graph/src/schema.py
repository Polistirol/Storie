# src/schema.py
"""
Schema del knowledge graph biografico.

Definisce i tipi di nodo, i tipi di arco, le regole di compatibilità,
e i modelli Pydantic per validare l'output dell'estrattore (stadio 3).

Le EXTRACTION_INSTRUCTIONS sono il "contratto semantico" che viene
iniettato nel prompt di estrazione. Modifiche qui = bump di SCHEMA_VERSION
= ri-estrazione dei chunk.

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
# Istruzioni di estrazione (testo iniettato nel prompt di stadio 3)
#
# Questo è il "contratto semantico". Modifiche qui = SCHEMA_VERSION va bumpato
# e i chunk vanno ri-estratti, perché l'output di Claude cambia.
# -----------------------------------------------------------------------------

EXTRACTION_INSTRUCTIONS = """
Estrai entità e relazioni dal paragrafo seguente di Memorie di Adriano
(Marguerite Yourcenar, traduzione italiana di Storoni Mazzolani).

TIPI DI NODO

- Person: una persona nominata o chiaramente identificabile.
  Usa la forma italiana canonica: Antinoo (non Antinous), Traiano (non Trajan),
  Plotina, Marco Aurelio. Adriano stesso è una Person.

- Event: un fatto accaduto nel tempo narrato della vita di Adriano.
  Battaglie, viaggi, incontri, morti, decisioni, riti. Sempre qualcosa che
  ACCADE in un momento situabile, anche vagamente.

- Place: un luogo geografico nominato o chiaramente implicato.
  Forma italiana canonica: Roma, Atene, Egitto, Villa Adriana.

- Phase: un periodo della vita di Adriano (giovinezza, principato, malattia
  terminale, ecc.) o un'era storica nominata. Si estrae SOLO se il testo la
  delimita in modo riconoscibile.

- Theme: un tema astratto su cui il testo insiste (la morte, il potere,
  l'amore, la memoria, il corpo). Estrai solo i temi davvero presenti nel
  paragrafo, non quelli generici del libro.

- Reflection: una considerazione, valutazione, sentenza del NARRATORE
  (Adriano vecchio che scrive a Marco Aurelio) SUI fatti. Vedi sotto.

- Work: un'opera, un edificio, uno scritto attribuibile a qualcuno.

DISTINZIONE CRITICA: Event vs Reflection
========================================
Memorie di Adriano è scritto in forma di lettera. Adriano vecchio e morente
guarda indietro alla propria vita. Il testo alterna continuamente:

  (a) il racconto di un fatto       -> Event
  (b) il commento del narratore sul fatto -> Reflection

Esempio:
  "Andai a caccia in Bitinia con Antinoo. Comprendo solo oggi che quei mesi
   furono il vertice della mia felicità."

  -> Event: "caccia in Bitinia con Antinoo"
  -> Reflection: "comprensione retrospettiva che quei mesi furono il vertice
     della felicità"
  -> Edge: la Reflection REFLECTS_ON l'Event.

Segnali linguistici di Reflection:
  - tempi verbali del presente o passato prossimo in mezzo a un passato remoto
  - prima persona valutativa: "comprendo", "ora so", "mi rendo conto", "ammetto"
  - sentenze gnomiche, massime generali
  - giudizi morali, estetici, filosofici espressi dal narratore

NON CONFONDERE il pensiero di Adriano-personaggio (parte dell'Event) con il
commento di Adriano-narratore (Reflection). Se l'enunciato si potrebbe
attribuire all'Adriano del momento, è dentro l'Event. Se richiede la
prospettiva del vecchio che scrive, è Reflection.

In caso di dubbio: estrai entrambi e marca la Reflection con confidence bassa.

TIPI DI ARCO

Fattuali:
  - INVOLVES        Event -> Person
  - LOCATED_AT      Event -> Place
  - DURING          Event -> Phase
  - CREATED         Person -> Work
  - RELATED_TO      jolly generico (usa solo se nessun altro arco si adatta)

Riflessivi / tematici:
  - EMBODIES        Event o Person -> Theme    (il fatto incarna il tema)
  - REFLECTS_ON     Reflection -> qualsiasi nodo
  - ECHOES          Event -> Event             (eco narrativo, ripresa)
  - CONTRASTS_WITH  Event<->Event, Theme<->Theme
  - TRANSFORMS_INTO Phase->Phase, Person->Person  (cambiamento interiore)

Causali / temporali:
  - CAUSED          Event -> Event
  - FOLLOWS         Event -> Event (successione)

REGOLE OPERATIVE

1. Estrai SOLO ciò che il paragrafo afferma o implica fortemente.
   Non aggiungere conoscenza storica esterna.

2. Per ogni nodo fornisci:
   - id: stringa breve e parlante in italiano, snake_case
     (es. "antinoo", "viaggio_in_egitto_130", "morte_di_traiano")
   - type
   - name: forma canonica italiana
   - description: 1-2 frasi che riassumono cosa il paragrafo dice DI questo nodo
   - aliases: eventuali varianti del nome trovate nel testo
   - confidence: 0.0-1.0
   - evidence_span: la sottostringa esatta del paragrafo che giustifica
     l'estrazione (citazione letterale, massimo 200 caratteri)

3. Per ogni arco:
   - source_id, target_id (devono comparire tra i nodi estratti)
   - type
   - description: opzionale, perché esiste questo arco
   - confidence
   - evidence_span: la sottostringa che giustifica la relazione

4. Se il paragrafo è puramente descrittivo e non offre entità nuove
   (es. una digressione astratta senza riferimenti concreti) restituisci
   nodi e archi vuoti. Non forzare estrazioni.

5. Output: JSON valido, conforme allo schema ExtractedGraph.
   Nessun commento, nessun testo fuori dal JSON.
"""


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