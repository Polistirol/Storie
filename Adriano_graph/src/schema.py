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

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "0.2.0"


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
    ERA = "Era"


# -----------------------------------------------------------------------------
# Era: id canonici a livello applicativo
#
# `Era` è un tipo di nodo a *set chiuso* nella semantica del dominio biografico:
# le quattro fasi maggiori della vita (infanzia, gioventù, adultità, vecchiaia)
# costituiscono la spina dorsale temporale del grafo. Servono come ancora
# temporale primaria per gli Event, accanto alle Phase emergenti dal testo.
#
# Lo schema Pydantic NON enforza il set chiuso sui `name`: il vincolo è solo
# applicativo. Questo per restare flessibili su altre vite/culture in futuro,
# dove le suddivisioni delle "ere di vita" possono essere diverse (es.
# narrazione clinica con fasi specifiche del paziente).
#
# La costante seguente documenta i quattro `id` canonici nel loro **ordine
# cronologico**, da usare in:
# - stadio 3.5: generazione deterministica dei nodi Era e delle catene
#   TRANSFORMS_INTO fra Era consecutive (infanzia → gioventù → adultità →
#   vecchiaia), senza chiederle al modello.
# - stadio 3 (prompt): elenco autoritativo dei valori ammessi per l'aggancio
#   Event → Era.
# -----------------------------------------------------------------------------

ERA_CANONICAL_IDS: tuple[str, ...] = (
    "infanzia",
    "gioventu",
    "adultita",
    "vecchiaia",
)


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

    OCCURS_IN = "OCCURS_IN"         # Phase -> Era


# -----------------------------------------------------------------------------
# INVOLVES: ruoli ammessi per la Person coinvolta in un Event
#
# Sotto PROMPT_VERSION 0.3.0 ogni INVOLVES era semanticamente piatto: una
# qualsiasi Person citata in relazione a un Event finiva collegata via
# INVOLVES senza distinzione fra protagonista, comparsa e semplice menzione.
# Questo gonfia il grado dei nodi e rende ambigue le query "chi *agisce*
# in questo Event?" vs "chi viene *citato* a margine?".
#
# In 0.2.0 / PROMPT_VERSION 0.4.0 ogni INVOLVES porta un `role` da un set
# chiuso applicativo:
# - "protagonist": Person che agisce o subisce l'Event come soggetto centrale.
# - "participant": Person presente alla scena con ruolo attivo ma non centrale.
# - "mentioned":   Person citata di sfuggita, senza agire nell'Event.
#
# Il vincolo è applicativo (validator Pydantic), non a livello di typing:
# `role` resta `Optional[str]` per mantenere la shape uniforme di Edge e
# permettere a tutti gli altri tipi di arco di omettere il campo. Il
# validator enforza che `role` sia in INVOLVES_ROLES se e solo se
# `type == INVOLVES`, e `None` altrimenti.
# -----------------------------------------------------------------------------

INVOLVES_ROLES: tuple[str, ...] = (
    "protagonist",
    "participant",
    "mentioned",
)


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
    EdgeType.DURING:          ({NodeType.EVENT}, {NodeType.PHASE,NodeType.ERA}),
    EdgeType.CREATED:         ({NodeType.PERSON}, {NodeType.WORK}),
    EdgeType.RELATED_TO:      (set(), set()),  # jolly: usabile, ma il prompt lo scoraggia
    EdgeType.EMBODIES:        ({NodeType.EVENT, NodeType.PERSON}, {NodeType.THEME}),
    EdgeType.REFLECTS_ON:     ({NodeType.REFLECTION}, set()),  # può riflettere su qualsiasi cosa
    EdgeType.ECHOES:          ({NodeType.EVENT}, {NodeType.EVENT}),
    EdgeType.CONTRASTS_WITH:  ({NodeType.EVENT, NodeType.THEME}, {NodeType.EVENT, NodeType.THEME}),
    EdgeType.TRANSFORMS_INTO: ({NodeType.PHASE, NodeType.PERSON, NodeType.ERA}, {NodeType.PHASE, NodeType.PERSON, NodeType.ERA}),
    EdgeType.CAUSED:          ({NodeType.EVENT}, {NodeType.EVENT}),
    EdgeType.FOLLOWS:         ({NodeType.EVENT,NodeType.ERA}, {NodeType.EVENT,NodeType.ERA}),
    EdgeType.OCCURS_IN:       ({NodeType.PHASE}, {NodeType.ERA}),

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


class Subject(Node):
    """
    Specializzazione di Node per il soggetto biografico del grafo (Adriano
    nel caso Yourcenar, il paziente in ambito di prodotto). Tutti i campi
    aggiuntivi sono opzionali per compatibilità all'indietro e portabilità
    fra domini.

    Architettura
    ------------
    I campi biografici di Subject (`birth_year`, `death_year`, `birth_place`,
    `surnames`, `biographical_notes`) NON sono estratti dal modello durante
    lo stadio 3. Sono **iniettati a configurazione**: il caller di stadio 3
    li carica da un file di profilo del soggetto (es.
    `data/subject_profile.json`) e costruisce il nodo Subject una volta sola
    all'inizio del run. Il modello vede invece quei dati condensati nel
    SYSTEM_PROMPT come contesto di ambientazione, non come campi da
    riempire.

    Razionale: le informazioni anagrafiche sono fattuali, stabili e note a
    priori. Chiederle al modello chunk per chunk produrrebbe varianti
    rumorose della stessa informazione. Iniettare il profilo nel prompt e
    creare il nodo a parte è più affidabile e risparmia token.

    Persistenza
    -----------
    Quando il grafo viene caricato in Neo4j (stadio 3.5), il nodo Subject
    riceve un'etichetta aggiuntiva `:Subject` oltre a `:Person`. Le viste
    del grafo possono usare `:Subject` per nascondere o evidenziare il
    soggetto stesso, che altrimenti diventa un hub gigantesco e soffoca la
    visualizzazione (nel run 0.3.0 Adriano aveva degree 854 contro 131 del
    secondo nodo).

    Vincoli
    -------
    `type` DEVE essere `NodeType.PERSON`: Subject è una specializzazione
    di Person, non un nuovo `NodeType`. Un grafo ha tipicamente un solo
    Subject (il soggetto della biografia), ma lo schema non lo vieta.
    """
    birth_year: Optional[int] = None              # anno di nascita
    death_year: Optional[int] = None              # anno di morte (None se vivente)
    birth_place: Optional[str] = None             # luogo di nascita: forma italiana canonica
    surnames: list[str] = Field(default_factory=list)  # cognomi/altri nomi (es. "Publio Elio Adriano")
    context_notes: Optional[str] = None      # note di contesto del cliente iniettate nel prompt come contesto
    biographical_notes: Optional[str] = None      # note libere iniettate nel prompt come contesto

    @field_validator("type")
    @classmethod
    def type_must_be_person(cls, v: NodeType) -> NodeType:
        if v != NodeType.PERSON:
            raise ValueError(
                f"Subject deve avere type=NodeType.PERSON, ricevuto {v!r}"
            )
        return v


class Edge(BaseModel):
    source_id: str
    target_id: str
    type: EdgeType
    description: Optional[str] = None  # opzionale: glossa testuale del motivo dell'arco
    role: Optional[str] = None         # obbligatorio per type==INVOLVES, None altrove (vedi validator)
    provenance: Provenance

    @model_validator(mode="after")
    def role_consistency_with_type(self) -> "Edge":
        """
        Coerenza cross-field fra `type` e `role`:
        - se `type == INVOLVES`, `role` deve essere uno dei valori in
          INVOLVES_ROLES (campo obbligatorio per quel tipo di arco).
        - per ogni altro `type`, `role` deve essere None (il campo esiste
          solo per uniformare la shape, non porta informazione utile).

        Validazione applicativa via model_validator: tiene `role` come
        Optional[str] generico, ma fallisce in costruzione se la
        combinazione type/role è incoerente. Non normalizza (es. no
        lowercase, no strip): valori non canonici falliscono loudly.
        """
        if self.type == EdgeType.INVOLVES:
            if self.role is None:
                raise ValueError(
                    "role è obbligatorio per gli archi INVOLVES; "
                    f"valori ammessi: {INVOLVES_ROLES}"
                )
            if self.role not in INVOLVES_ROLES:
                raise ValueError(
                    f"role {self.role!r} non ammesso per INVOLVES; "
                    f"valori ammessi: {INVOLVES_ROLES}"
                )
        else:
            if self.role is not None:
                raise ValueError(
                    f"role deve essere None per archi di tipo {self.type.value!r}, "
                    f"ricevuto {self.role!r}"
                )
        return self


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


def is_involves_role_valid(role: Optional[str], edge_type: EdgeType) -> bool:
    """
    Validazione post-estrazione del campo `role` rispetto al tipo di arco.
    Specchio non-bloccante del `model_validator` su `Edge.role`: NON solleva
    eccezioni, restituisce solo un booleano.

    Da usare in stadio 3 dopo il parsing dell'output del modello, quando
    si vuole *loggare* i casi anomali e poi decidere caso per caso
    (skippare l'arco, riassegnare un default come "participant", ecc.)
    invece di far fallire l'intera estrazione del chunk.

    Regole (uguali al validator di Edge):
    - `edge_type == INVOLVES` → `role` deve essere uno dei valori in
      `INVOLVES_ROLES` (e quindi non None).
    - `edge_type != INVOLVES` → `role` deve essere None.

    Casi tipici di anomalia che questa funzione cattura per il logging:
    - il modello produce un INVOLVES senza role → False (regola dimenticata).
    - il modello assegna un role su un LOCATED_AT/DURING/ecc. → False
      (regola applicata troppo).
    - il modello inventa un role nuovo ("witness", "victim") → False
      (set chiuso non rispettato).
    """
    if edge_type == EdgeType.INVOLVES:
        return role in INVOLVES_ROLES
    return role is None