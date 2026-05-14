# src/stage_3_prompt.py
"""
Prompt di estrazione per lo stadio 3.

Responsabilità: costruire i payload per la chiamata a Claude (system prompt,
messages multi-turn con few-shot, tool definition per output strutturato).

Separato da schema.py: qui vive il "contratto semantico" verso il modello.
schema.py resta puro: enum, Pydantic, EDGE_COMPATIBILITY.

Versioning sdoppiato:
- SCHEMA_VERSION (in schema.py): shape strutturale dell'output.
- PROMPT_VERSION (qui): contratto semantico, istruzioni, esempi.
Entrambi finiscono in Provenance. Bump di PROMPT_VERSION = ri-estrazione
consigliata se vuoi confrontare run omogenei.

Convenzione output del modello (vedi ADR-012):
- Il tool `submit_extraction` accetta nodi e archi in formato "flat":
  i campi `confidence` ed `evidence_span` sono top-level, NON dentro un
  oggetto `provenance`.
- I campi tecnici di Provenance (chunk_id duplicato, model, timestamp,
  schema_version, human_validated) NON vengono richiesti al modello.
  Sono arricchiti dal chiamante in stage_3_extract.py al momento del
  wrapping in `Node`/`Edge` Pydantic.
- I file few-shot in `data/stage_3/few_shots/` sono invece annotati a
  mano nel formato "fat" (Provenance annidata) per coerenza con il
  modello dati finale. La funzione `load_few_shot_examples()` fa il
  bridge tra i due formati al caricamento.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src.schema import SCHEMA_VERSION  # noqa: F401  (esportato per provenance)

PROMPT_VERSION = "0.1.0"

# Se True, le `description` di nodi e archi vengono OMESSE dal formato flat
# dei few-shot passato al modello. Default False per non degradare la qualità
# semantica del few-shot, specialmente in vista di modelli più piccoli
# (Sonnet basic, 7-8B locali) dove le description fungono da "training in
# context" del tono descrittivo. Su modelli grandi (Sonnet 4.6) se un primo
# run a False risulta convincente si può alzare a True per risparmiare
# ~3-4k token (vedi ADR-013).
# La costante è letta da _flatten_node/_flatten_edge al momento del caricamento
# dei few-shot: cambiarla richiede un re-import del modulo (o un re-run).
REMOVE_DESCRIPTION = False


# -----------------------------------------------------------------------------
# Path delle risorse (relativi al modulo, indipendenti dal cwd).
# -----------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent
_FEW_SHOTS_DIR = _PROJECT_ROOT / "data" / "stage_3" / "few_shots"
_CHUNKS_JSON = _PROJECT_ROOT / "data" / "stage_2" / "chunks.json"

# Ordine fissato per riproducibilità: la sequenza dei few-shot conta sia per
# il caching del prompt sia per pedagogia (Event puro -> meditativo -> misto).
_FEW_SHOT_FILES: tuple[str, ...] = (
    "ch_0047.json",   # Ritratto di Traiano: Event + Reflection + Theme
    "ch_0122.json",   # Gestione del patrimonio imperiale: Event + Reflection
    "ch_0113.json",   # Meditazione Grecia/Roma: chunk meditativo, molte Reflection
    "ch_0127.json",   # Esecuzione di Akiba e Fusco: caso etico denso
)


# -----------------------------------------------------------------------------
# System prompt: ruolo, contratto semantico, regole.
#
# Va nel blocco `system` della chiamata API. Stabile attraverso i 310 chunk:
# è il candidato naturale per il cache breakpoint.
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = """\
# Ruolo e contesto

Sei un estrattore di knowledge graph biografico. Devi estrarre nodi
(entità) e archi (relazioni) da un singolo paragrafo del seguente testo:

*Memorie di Adriano*, di Marguerite Yourcenar, nella traduzione italiana
di Lidia Storoni Mazzolani. Il narratore è Adriano vecchio e morente
che scrive a Marco Aurelio: ricorda la propria vita e la commenta dalla
fine.

# Tipi di nodo

- Person: persona nominata o chiaramente identificabile.
  Usa la forma italiana canonica: Antinoo (non Antinous), Traiano (non
  Trajan), Plotina, Marco Aurelio. Adriano stesso è una Person.

- Event: fatto accaduto nel tempo narrato della vita di Adriano.
  Battaglie, viaggi, incontri, morti, decisioni, riti. Sempre qualcosa
  che ACCADE in un momento situabile, anche vagamente. Anche atti
  interiori (un'esitazione, una decisione) sono Event se sono situati
  nel tempo del personaggio.

- Place: luogo geografico nominato o chiaramente implicato.
  Forma italiana canonica: Roma, Atene, Egitto, Villa Adriana.
  Anche un luogo archetipico (acropoli greca generica) va estratto come
  Place, con descrizione che chiarisce il valore generico.

- Phase: un periodo della vita di Adriano (giovinezza, principato,
  malattia terminale) o un'era storica nominata. Estrai SOLO se il testo
  la delimita in modo riconoscibile.

- Theme: un tema astratto su cui il testo INSISTE (la morte, il potere,
  l'amore, la memoria, il corpo). Estrai solo i temi davvero presenti
  nel paragrafo con una manifestazione concreta, non quelli generici
  del libro. Pochi Theme per chunk: se ne stai trovando cinque, stai
  sovra-estraendo.

- Reflection: una considerazione, valutazione o sentenza del NARRATORE
  (Adriano vecchio che scrive) SUI fatti. Vedi distinzione critica sotto.

- Work: un'opera, un edificio, uno scritto attribuibile a qualcuno.

# Distinzione critica: Event vs Reflection

*Memorie di Adriano* è scritto in forma di lettera. Il testo alterna
continuamente:

  (a) il racconto di un fatto              -> Event
  (b) il commento del narratore sul fatto  -> Reflection

Esempio:
  "Andai a caccia in Bitinia con Antinoo. Comprendo solo oggi che quei
   mesi furono il vertice della mia felicità."

  -> Event:      "caccia in Bitinia con Antinoo"
  -> Reflection: "comprensione retrospettiva che quei mesi furono il
                  vertice della felicità"
  -> Edge:       la Reflection REFLECTS_ON l'Event.

Segnali linguistici di Reflection:
- tempi al presente o passato prossimo in mezzo a un passato remoto
- prima persona valutativa: "comprendo", "ora so", "mi rendo conto",
  "ammetto", "oso"
- sentenze gnomiche, massime generali, presente gnomico
- giudizi morali, estetici, filosofici espressi dal narratore
- condizionali del rimpianto: "avrei voluto", "avrei dovuto"

NON confondere il pensiero di Adriano-personaggio (parte dell'Event)
con il commento di Adriano-narratore (Reflection). Se l'enunciato si
potrebbe attribuire all'Adriano del momento, è dentro l'Event. Se
richiede la prospettiva del vecchio che scrive, è Reflection.

In caso di dubbio: estrai entrambi e marca la Reflection con confidence
bassa.

# Tipi di arco

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
- FOLLOWS         Event -> Event (successione temporale)

# Regole operative

1. Estrai SOLO ciò che il paragrafo afferma o implica fortemente.
   Non aggiungere conoscenza storica esterna al testo.

2. Per ogni nodo fornisci:
   - id: stringa breve e parlante in italiano, snake_case
     (es. "antinoo", "viaggio_in_egitto_130", "morte_di_traiano")
   - type
   - name: forma canonica italiana
   - description: 1-2 frasi che riassumono cosa il paragrafo dice DI
     questo nodo
   - aliases: eventuali varianti del nome trovate nel testo (lista
     anche vuota)
   - confidence: 0.0-1.0
   - evidence_span: la sottostringa esatta del paragrafo che giustifica
     l'estrazione (citazione letterale, massimo ~200 caratteri)

3. Per ogni arco:
   - source_id, target_id (devono comparire tra i nodi estratti)
   - type
   - description: opzionale, perché esiste questo arco
   - confidence: 0.0-1.0
   - evidence_span: la sottostringa che giustifica la relazione

4. Se il paragrafo è puramente descrittivo e non offre entità nuove
   (es. una digressione astratta senza riferimenti concreti) restituisci
   nodi e archi vuoti. NON forzare estrazioni.

5. Sotto-estrai prima di sovra-estrarre. Meglio un grafo sparso e fedele
   di uno denso e inventato. La provenance (evidence_span letterale) è
   non negoziabile: se non riesci a indicare la sottostringa che la
   giustifica, l'estrazione non esiste.

6. I nodi isolati sono legittimi. Una Person citata di sfuggita senza
   relazioni nel chunk è informazione, non un bug.

# Output

Restituisci l'estrazione invocando il tool `submit_extraction` con:
- `chunk_id`: copia letterale dell'id ricevuto in input
- `nodes`: lista di nodi nel formato sopra
- `edges`: lista di archi nel formato sopra

NON aggiungere testo fuori dal tool. NON commentare a parte. Tutto ciò
che hai estratto va dentro l'invocazione del tool.

I campi tecnici di provenance (model, timestamp, schema_version) sono
aggiunti automaticamente dal sistema a valle: a te bastano `confidence`
ed `evidence_span` per ogni nodo e per ogni arco.
"""


# -----------------------------------------------------------------------------
# Caricamento e trasformazione degli esempi few-shot.
#
# I file in `data/stage_3/few_shots/*.json` sono annotati a mano nel formato
# "fat" del knowledge graph finale (con Provenance annidata, human_validated,
# ecc.). Il modello, invece, deve produrre il formato "flat" definito da
# EXTRACTION_TOOL. La trasformazione avviene qui, una sola volta all'import.
# -----------------------------------------------------------------------------

def _flatten_node(node: dict) -> dict:
    """Estrae da un Node fat (con `provenance` annidata) la shape flat del tool."""
    prov = node.get("provenance", {})
    out = {
        "id": node["id"],
        "type": node["type"],
        "name": node["name"],
        "confidence": prov["confidence"],
        "evidence_span": prov["evidence_span"],
    }
    if not REMOVE_DESCRIPTION and node.get("description"):
        out["description"] = node["description"]
    if node.get("aliases"):
        out["aliases"] = node["aliases"]
    return out


def _flatten_edge(edge: dict) -> dict:
    """Estrae da un Edge fat la shape flat del tool."""
    prov = edge.get("provenance", {})
    out = {
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "type": edge["type"],
        "confidence": prov["confidence"],
        "evidence_span": prov["evidence_span"],
    }
    if not REMOVE_DESCRIPTION and edge.get("description"):
        out["description"] = edge["description"]
    return out


@lru_cache(maxsize=1)
def _chunk_text_index() -> dict[str, str]:
    """Indice chunk_id -> text caricato una sola volta da chunks.json."""
    with _CHUNKS_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {c["chunk_id"]: c["text"] for c in data["chunks"]}


def _load_one_example(file_name: str) -> dict:
    """Carica un singolo file few-shot e lo trasforma nel formato per build_messages."""
    file_path = _FEW_SHOTS_DIR / file_name
    # Il chunk_id reale è il nome del file senza estensione (es. "ch_0047").
    # NB: il campo `chunk_id` interno al JSON è una stringa di lavoro
    # dell'annotatore (es. "yourcenar_p2_ch_0047") e viene ignorato: la
    # fonte di verità è il filesystem.
    chunk_id = file_path.stem

    with file_path.open("r", encoding="utf-8") as f:
        annotated = json.load(f)

    chunk_text = _chunk_text_index()[chunk_id]

    extraction = {
        "chunk_id": chunk_id,
        "nodes": [_flatten_node(n) for n in annotated.get("nodes", [])],
        "edges": [_flatten_edge(e) for e in annotated.get("edges", [])],
    }
    return {
        "chunk_id": chunk_id,
        "chunk_text": chunk_text,
        "extraction": extraction,
    }


def load_few_shot_examples() -> list[dict]:
    """
    Restituisce i 4 esempi few-shot pronti per build_messages().
    Ogni elemento: {"chunk_id", "chunk_text", "extraction": {chunk_id, nodes, edges}}.
    """
    return [_load_one_example(name) for name in _FEW_SHOT_FILES]


# Bind eager: l'import del modulo carica e valida gli esempi.
# Se mancano file o `chunks.json`, fallisce subito (comportamento desiderato:
# il modulo non ha senso senza esempi).
FEW_SHOT_EXAMPLES: list[dict] = load_few_shot_examples()


# -----------------------------------------------------------------------------
# Tool definition: forza output strutturato.
#
# Lo schema JSON qui deve corrispondere alla shape "flat" attesa dal modello,
# che è un sottoinsieme di ExtractedGraph di schema.py: il caller (stage_3_extract)
# arricchirà con Provenance prima di costruire Node/Edge Pydantic.
# -----------------------------------------------------------------------------

EXTRACTION_TOOL = {
    "name": "submit_extraction",
    "description": (
        "Sottometti l'estrazione di nodi e archi dal chunk fornito. "
        "Conforme allo schema ExtractedGraph del knowledge graph biografico."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chunk_id": {
                "type": "string",
                "description": "id del chunk in esame, copiato dall'input.",
            },
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "Person", "Event", "Place", "Phase",
                                "Theme", "Reflection", "Work",
                            ],
                        },
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "evidence_span": {
                            "type": "string",
                            "description": "citazione letterale dal chunk, max ~200 caratteri.",
                        },
                    },
                    "required": ["id", "type", "name", "confidence", "evidence_span"],
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "target_id": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "INVOLVES", "LOCATED_AT", "DURING", "CREATED",
                                "RELATED_TO", "EMBODIES", "REFLECTS_ON",
                                "ECHOES", "CONTRASTS_WITH", "TRANSFORMS_INTO",
                                "CAUSED", "FOLLOWS",
                            ],
                        },
                        "description": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "evidence_span": {"type": "string"},
                    },
                    "required": [
                        "source_id", "target_id", "type",
                        "confidence", "evidence_span",
                    ],
                },
            },
        },
        "required": ["chunk_id", "nodes", "edges"],
    },
}


# -----------------------------------------------------------------------------
# Builder dei messages.
#
# Restituisce la lista da passare a `messages` nella chiamata API.
# Struttura:
#   [
#       user(esempio_1_chunk), assistant(tool_use con esempio_1_extraction),
#       user(tool_result),
#       user(esempio_2_chunk), assistant(tool_use con esempio_2_extraction),
#       user(tool_result),
#       ...
#       user(tool_result_ULTIMO_ESEMPIO con cache_control)  <-- breakpoint cache #2
#       user(chunk_corrente),                                <-- la sola parte NON cachata
#   ]
#
# Il system prompt è passato a parte nel parametro `system` della chiamata API,
# con cache_control sul SYSTEM_PROMPT (breakpoint cache #1, vedi
# build_request_payload e ADR-013).
# -----------------------------------------------------------------------------

def _format_chunk_user_message(chunk_id: str, chunk_text: str) -> dict:
    """Formato standard del turno user: chunk_id + testo. Stesso in esempi e in produzione."""
    return {
        "role": "user",
        "content": (
            f"chunk_id: {chunk_id}\n\n"
            f"chunk_text:\n{chunk_text}"
        ),
    }


def _format_assistant_tool_use(extraction: dict) -> dict:
    """Turno assistant: invocazione del tool submit_extraction con l'estrazione attesa."""
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": f"toolu_example_{extraction['chunk_id']}",
                "name": "submit_extraction",
                "input": extraction,
            }
        ],
    }


def _format_user_tool_result(tool_use_id: str, with_cache: bool = False) -> dict:
    """
    Dopo ogni assistant tool_use serve un tool_result per chiudere il turno,
    altrimenti l'API rifiuta la sequenza. In few-shot lo lasciamo vuoto / ok.

    `with_cache=True` aggiunge il cache breakpoint su questo content block:
    si usa solo sull'ultimo few-shot per cachare l'intero prefisso (system +
    esempi). Vedi ADR-013.
    """
    block: dict = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": "ok",
    }
    if with_cache:
        block["cache_control"] = {"type": "ephemeral"}
    return {
        "role": "user",
        "content": [block],
    }


def build_messages(chunk_id: str, chunk_text: str) -> list[dict]:
    """
    Costruisce la lista `messages` per estrarre dal chunk corrente,
    preceduta dai few-shot in formato multi-turn.

    Il cache breakpoint #2 è piazzato sull'ultimo `tool_result` del 4° esempio,
    in modo che tutto il prefisso stabile (SYSTEM_PROMPT + tool schema +
    4 esempi) sia cachabile attraverso le 310 chiamate. La parte non
    cachata è solo l'ultimo `chunk_user_message` (~500-1000 token).
    """
    messages: list[dict] = []
    last_idx = len(FEW_SHOT_EXAMPLES) - 1

    for i, ex in enumerate(FEW_SHOT_EXAMPLES):
        tool_use_id = f"toolu_example_{ex['chunk_id']}"
        messages.append(_format_chunk_user_message(ex["chunk_id"], ex["chunk_text"]))
        messages.append(_format_assistant_tool_use(ex["extraction"]))
        messages.append(_format_user_tool_result(tool_use_id, with_cache=(i == last_idx)))

    messages.append(_format_chunk_user_message(chunk_id, chunk_text))
    return messages


# -----------------------------------------------------------------------------
# Builder del payload completo per anthropic.messages.create().
#
# Pensato per essere consumato da stage_3_extract.py senza che quello debba
# sapere nulla del contenuto del prompt.
#
# Prompt caching (ADR-013): due cache_control breakpoint, entrambi `ephemeral`
# (~5 min TTL, sufficiente per una run sequenziale sui 310 chunk).
#   #1: sul SYSTEM_PROMPT (qui sotto).
#   #2: sul tool_result dell'ultimo few-shot (in build_messages).
# Effetto: ~14k token di prompt fisso pagati pieni una sola volta, poi letti
# da cache al ~10% del costo. La sola parte non cachata per chunk è il
# chunk_user_message corrente.
# -----------------------------------------------------------------------------

def build_request_payload(chunk_id: str, chunk_text: str, model: str) -> dict:
    """
    Restituisce un dict pronto per `client.messages.create(**payload)`.
    Il caller aggiunge max_tokens, temperature, eventuali altri parametri.
    """
    return {
        "model": model,
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cache breakpoint #1
            }
        ],
        "tools": [EXTRACTION_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_extraction"},
        "messages": build_messages(chunk_id, chunk_text),
    }
