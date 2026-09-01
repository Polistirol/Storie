# Pipeline — Prototipo Knowledge Graph Biografico
#production

> **Progetto**: trasformare la storia di vita di una persona in un knowledge graph navigabile e un agente conversazionale in prima persona.
>
> **Prodotto**: il cliente racconta la propria biografia in sessioni registrate; la trascrizione alimenta questa pipeline, che produce il grafo esplorabile e l'indice per l'LLM. Deliverable: biografia premium navigabile.
>
> **Banco di prova**: *Memorie di Adriano* di Marguerite Yourcenar (trad. Storoni Mazzolani) — sorgente PDF al posto di registrazione/trascrizione, stessi stadi a valle.
>
> **Documento vivo**: aggiornare ad ogni cambio di stato o decisione. Le decisioni di design vanno in coda come ADR datati, non vengono cancellate.

---

## Stato globale

| Stadio | Nome | Stato | Output principale |
|---|---|---|---|
| 0 | `stage_0_extract_pdf` | fatto (v0.2.0) | `raw_text.txt` + `structure.json` + `extraction_log.json` |
| 1 | `stage_1_clean` | fatto (v0.1.0) | `cleaned_text.txt` + `cleaning_log.json` + `inspection_report.json` |
| 2 | `stage_2_chunk` | fatto (v0.1.0) | `chunks.json` + `chunking_log.json` |
| 3 | `stage_3_extract` (sub-stages `3-1_prompt`, `3-2_extract`) | fatto prompt v0.4.2, schema 0.2.0 |  `extracted_graph.json`  |
| 3.5 | `stage_3_5_load_to_neo4j` | fatto | grafo su Neo4j, via neo4j desktop, NO python ora |
| 4 | `stage_4_resolve` (+ `4-5` health_checkup; `4-4` structure opzionale) | fatto | `resolved_graph.json` + checkpoint qualità |
| 5 | `stage_5_enrich` (sub-stages `5-1` … `5-5` + `5-6` health_checkup) | fatto e validato (schema 0.3.0, dedup 0.2.0; checkup `pass_with_warnings`) | grafo arricchito (EMBODIES, gerarchia tematica, ECHOES, TRANSFORMS_INTO) |
| 6 | `stage_6_index` (sub-stages `6-1` index, `6-2` health_checkup) | in corso (build + checkup fatti, v0.1.0) | indice RAG ibrido in `data/stage_6/` |

Legenda stato: `da fare` · `in corso` · `fatto` · `bloccato` · `rivisto`

---

## Principi trasversali (validi per tutti gli stadi)

1. **Idempotenza**: rilanciare uno stadio non duplica nulla. Output deterministico a parità di input + parametri + versione.
2. **Provenienza**: ogni record (chunk, nodo, arco) porta con sé `source`, `stage_version`, `model` (se LLM), `timestamp`, `confidence` (se LLM), `human_validated` (bool).
3. **Versioning dello stadio**: ogni stadio ha una `stage_version` (semver). Cambia → output marcato → stadi a valle sanno che devono rielaborare.
4. **Log esplicito**: ogni stadio scrive un `<stage>_log.json` con conteggi, parametri usati, eventuali warning. Non si "perde" niente in silenzio.
5. **Italiano**: testo, prompt, nomi canonici (Antinoo, Traiano, Plotina).
6. **Niente over-engineering**. Funzionante > perfetto. Decisioni "pragmatiche" vanno annotate come ADR così non diventano debito tecnico nascosto.

---

## Stadio 0 — Estrazione PDF

**Scopo**: leggere il PDF text-based e produrre un `.txt` il più fedele possibile alla fonte, conservando struttura (parti, paragrafi) e segnalando anomalie.

**Stato**: fatto, versione `0.2.0`.

**Input**: `data/memorie_adriano.pdf`
**Output**:
- `data/stage_0/raw_text.txt`
- `data/stage_0/structure.json` (offset delle sei parti)
- `data/stage_0/extraction_log.json`
- `data/stage_0/intermediates/*` (snapshot di debug, controllati da `SAVE_INTERMEDIATES`)

**Cosa fa**:
- Estrae testo pagina-per-pagina con `pdfplumber` (trasparente, ispezionabile). Lavora solo sulle pagine elencate fuori da `IGNORE_PAGES` (lista esplicita per saltare copertina, indice, appendice critica).
- Risolve sillabazione di fine riga (`im-\nperatore` → `imperatore`), preservando trattini legittimi e trattini di inciso tipografico.
- Normalizza legature tipografiche (`ﬁ` → `fi`, `ﬂ` → `fl`, ecc.), spazi non-breaking e zero-width.
- **Rileva i paragrafi via indentazione tipografica** (coordinate `x0` delle parole via `extract_words`, mediana per pagina, soglia di rientro a `mediana + 5pt`). Mantiene anche la riga vuota come segnale OR. Vedi ADR-005.
- Identifica le sei parti del libro (match esatto sui titoli maiuscoli noti) e ne registra gli offset in `structure.json`. NIENTE marker inline.
- Cerca header/footer ricorrenti (assenti in questa edizione).

**Numeri reali su Yourcenar**: 170 pagine processate, ~521k caratteri, 322 paragrafi (~1.9 per pagina, coerente con il layout).

**Percorso produzione** (non ora): sostituire questo stadio con `stage_0_transcribe` — audio delle sessioni del cliente → trascrizione grezza + metadati (speaker, timestamp, pause). Gli stadi 1–7 restano invariati.

**Espansioni future** (non ora):
- Supporto a PDF con note a piè di pagina.
- Supporto a PDF con OCR (image-based) tramite `tesseract`.
- Estrazione di metadati strutturali (TOC, autore, ecc.) per progetti con più volumi.

---

## Stadio 1 — Cleaning

**Scopo**: applicare correzioni deterministiche al testo grezzo, con log ispezionabile di ogni modifica. Slot architetturale che resta nella pipeline anche quando vuoto (servirà per le trascrizioni da oral history).

**Stato**: fatto, versione `0.1.0`.

**Input**: `data/stage_0/raw_text.txt`
**Output**:
- `data/stage_1/cleaned_text.txt`
- `data/stage_1/cleaning_log.json`
- `data/stage_1/inspection_report.json` (diagnostica del testo, sempre attiva)

**Cosa fa**:
- Applica un dizionario di regole regex in `config/cleaning_rules.yaml` (dichiarativo: pattern, sostituzione, descrizione, `enabled` boolean).
- Per ogni sostituzione applicata, logga: offset, contesto ±40 caratteri, regola usata. Tracciabilità totale.
- **In parallelo**, indipendentemente dalle regole, fa un'ispezione diagnostica del testo cercando pattern sospetti: `digit_in_word`, `lonely_digit_sequence`, `rare_chars`, `repeated_punctuation`, `isolated_capitals`. Output in `inspection_report.json`. NON include "maiuscole interne" (i nomi propri storici nel testo possono averle legittimamente).
- L'ispezione **non modifica** il testo: è strumento di scoperta per compilare nuove regole, non azione automatica.

**Stato attuale dizionario per Yourcenar/PDF**: tutte le regole pre-popolate sono `enabled: false`. L'ispezione ha rilevato solo casi innocui (numeri di data, puntini di sospensione stilistici di Yourcenar). Nessuna regola attivata. Il `cleaned_text.txt` è copia byte-per-byte di `raw_text.txt`.

**Per le biografie cliente**: qui vivranno le correzioni domain-specific (nomi propri ricorrenti, toponimi mal trascritti, filler e ripetizioni da normalizzare). Lo slot è pronto.

**Espansioni future**:
- Validazione opzionale via diff visivo.
- Modalità "dry run".

---

## Stadio 2 — Chunking

**Scopo**: dividere il testo pulito in unità narrative coerenti per l'estrazione a valle.

**Stato**: fatto, versione `0.1.0`.

**Input**: `data/stage_1/cleaned_text.txt` + `data/stage_0/structure.json`
**Output**:
- `data/stage_2/chunks.json`
- `data/stage_2/chunking_log.json`

**Strategia** (decisa in ADR-006, raffinamento di ADR-002):
- **Regola base**: 1 chunk = 1 paragrafo. Massima granularità della provenienza, massimo rispetto della struttura autoriale.
- **Eccezione di accorpamento**: paragrafi sotto `MIN_TOKENS = 80` vengono accorpati al successivo della stessa parte (o al precedente se sono l'ultimo della parte). NIENTE accorpamento transitivo (limite a coppia singola).
- **Nessuna soglia massima**: i paragrafi lunghi restano interi.
- **NIENTE overlap, NIENTE sliding window**.
- **Confini duri sulle sei parti**: un chunk non attraversa mai un confine di parte (offset letti da `structure.json`).
- **Filtraggio titoli**: i sei titoli delle parti sono metadati strutturali, esclusi dal corpus dei chunk.

**Metadati per chunk**:
- `chunk_id` (`ch_0001`, zero-padded, numerazione globale progressiva)
- `part` (numero + titolo)
- `position_in_part`, `position_global`
- `char_start`, `char_end` (offset nel `cleaned_text.txt`)
- `token_count` (tiktoken `cl100k_base` come proxy)
- `paragraph_indices` (lista, sempre — anche con un solo elemento)
- `merged` (bool)
- `text` (contenuto del chunk)

**Esplicitamente NON incluso**: `narrative_phase`. Emerge come entità Phase nello stadio 3 (ADR-001).

**Numeri reali su Yourcenar**: 322 paragrafi → 6 titoli filtrati → 316 candidati → 6 accorpamenti → **310 chunk finali**. Statistiche token: min 95, max 1265, mediana 478, p95 929. Tutti i chunk verificati a campione come prosa coerente.

**Espansioni future**:
- Eventuale ri-chunking semantico se gli esperimenti mostrano perdita di legami `REFLECTS_ON` o `ECHOES`.
- Hash di contenuto per re-processing selettivo (premature optimization per ora).

---

## Stadio 3 — Estrazione entità e relazioni

**Scopo**: per ogni chunk, estrarre nodi (Person, Event, Place, Phase, Theme, Reflection, Work) e archi (INVOLVES, LOCATED_AT, REFLECTS_ON, ECHOES, ecc.) usando lo schema in `src/schema.py`.

**Stato**: da fare, **prototipo first**.

**Input**: `data/stage_2/chunks.json` (selezione di 5-10 chunk per il prototipo)
**Output**:
- `data/stage_3/full_runs/<datetime>/extracted_graph.json` — **JSON intermedio**, NON scrittura diretta su Neo4j
- `data/stage_3/3_health_checkup/` — checkpoint post-run (`dashboard.html`, `metrics.json`, `checks.json`, `review_queue.json`, `health_log.json`); metriche via `tools/extraction_analysis.py`, orchestrate da `src/stage_3-3_health_checkup.py`

**Strategia** (vedi ADR-007, ADR-008, ADR-009):
- **Prompt diretto a Claude Sonnet 4.6** via SDK ufficiale `anthropic`. NIENTE LlamaIndex.
- **Few-shot prompting** con esempi versionati in file separato (`config/extraction_examples.yaml`), scritti a mano su chunk veri del libro.
- **1 chunk = 1 chiamata API** durante il prototipo.
- **Validazione Pydantic** dell'output JSON contro lo schema.
- **Persistenza disaccoppiata**: il caricamento su Neo4j sarà uno stadio 3.5 separato. Permette di rilanciare il caricamento senza ri-estrarre, e di ispezionare il grafo come testo.
- **Provenienza arricchita** per ogni nodo/arco: `chunk_id`, `model`, `timestamp`, `confidence`, `evidence_span` (offset nel chunk dove l'estrattore ha visto l'evidenza), `human_validated: false` di default.
- **Distinzione Event vs Reflection** esplicitata sia nelle istruzioni del prompt sia negli esempi few-shot (è il caso critico).

**Workflow del prototipo**:
1. Selezione di 5-10 chunk per varietà (Event puro, Reflection puro, misti, apertura di parte, densità entità).
2. Scrittura a mano di 3-5 esempi few-shot annotati (2-3 di questi vengono dai chunk selezionati).
3. Design del prompt di estrazione in italiano (schema esplicito + esempi + istruzioni Event/Reflection).
4. Implementazione di `src/stage_3_extract.py` con funzione `extract_from_chunk(chunk, schema, examples)` (~100 righe).
5. Esecuzione sul campione, ispezione qualitativa.
6. Iterazione su prompt ed esempi finché la qualità è soddisfacente.
7. Solo allora: estensione a tutti i 310 chunk.

**Da decidere durante il design del prototipo**:
- ID dei nodi: generati dal modello o costruiti deterministicamente dal codice?
- `confidence`: richiesta al modello (auto-stima notoriamente imprecisa) o omessa?
- `evidence_span`: richiesta sempre o opzionale?

---

## Stadio 3.5 — Caricamento su Neo4j

**Scopo**: prendere `extracted_graph.json` e caricarlo nel database Neo4j locale, applicando vincoli di schema e indici.

**Stato**: da definire dopo lo stadio 3.

**Razionale**: separare estrazione (costosa, LLM) da persistenza (deterministica, ripetibile) protegge il prototipo dal dover ri-spendere chiamate API quando il caricamento ha bug. Idempotenza piena, costi separati.

---
## Stadio 4 — Resolve / Deduplica
**Scopo**: prendere extracted_graph.json (output chunk-locale dello stadio 3) e produrre resolved_graph.json, un grafo in cui ogni entità del mondo è un solo nodo canonico, con tutte le sue provenienze accorpate, e mappe ispezionabili di ogni decisione di merge e split.
**Stato**: fatto, versione 0.1.0.
**Input**: data/stage_3/full_runs/<datetime>/extracted_graph.json
**Output**:

`data/stage_4/diagnostics/nodes_index.json (indice scarno + conteggi)`
`data/stage_4/diagnostics/type_collisions.json (id con type discordante)`
`data/stage_4/diagnostics/name_duplicates.json (gruppi exact + near)`
`data/stage_4/merge_map.json (decisioni di merge, sezioni auto e to_review)`
`data/stage_4/split_map.json (decisioni di split)`
`data/stage_4/3_resolve/resolved_graph.json` — **fonte di verità** (grafo deduplicato completo, validato Pydantic)
`data/stage_4/3_resolve/resolver_log.json` — diario merge/split, warning, stats, proposte enrich
`data/stage_4/5_health_checkup/dashboard.html` (report visivo — aprire per primo), `checks.json`, `metrics.json`, `review_queue.json`, `health_log.json`

**Output opzionale** (solo esplorazione / file snelli, non usati dagli stadi a valle):

`data/stage_4/4_structure/nodes.json`, `edges.json`, `provenance.json` — viste piatte da `stage_4-4_structure.py`

**Strategia** (decisa in ADR-020, estesa in ADR-021, ADR-022):

Pipeline a fasi indipendenti + resolver finale. Fase 0 (diagnostica) → Fase A (merge) → Fase B (split) → Resolver (3) → Health checkup (5). Structure (4) è **opzionale**: proietta `resolved_graph.json` in file più leggeri per ispezione manuale. Ogni fase produce una mappa di decisioni ispezionabile; nessuna applica modifiche al grafo. Il resolver è l'unico punto di applicazione: legge le mappe e accorpa. La sezione `to_review` di `merge_map.json` serve a fissare `canonical_id` **durante** lo stadio 4 (decisione ontologica pre-resolve), non è coda di validazione a valle.
Fase 0 deterministica: dal grafo grezzo derivanti artefatti diagnostici leggeri (nessun caricamento del grafo intero in memoria oltre la lettura iniziale).
Fase A — merge per name identico: per ogni gruppo di id con stesso (type, name) normalizzato (strip/casefold/spazi), vince l'id con più occorrenze nel grafo (frequenza = canonicità d'uso). Niente euristiche linguistiche sul name. Pareggi, conflitti multi-direzione, e type-discordanti vanno in to_review con canonical_id da decidere a mano.
Fase B — split deterministico: per ogni id con type discordante, ogni occorrenza viene rietichettata <id>__<type_lower>. Nessuna whitelist, nessun LLM, nessuna regex linguistica. Le collisioni sono noise residuo dell'estrazione (~11 su 310 chunk), non un fenomeno semantico.
Resolver: preflight check, costruisce una rewrite map (id_originale, type) → id_canonico applicando split prima, merge dopo. Accorpa nodi per (id_canonico, type) raccogliendo tutte le provenances; name e description scelte per frequenza, mai riscritte. Accorpa archi per (source, target, type, role); valorizza `source_type` e `target_type` dai nodi canonici e valida `ResolvedGraph` Pydantic. Per gli split Event/Theme e Phase/Theme genera Stage6Proposal di tipo EMBODIES, tracciate in `resolver_log.json` ma non come archi del grafo.

**Modelli**: i nodi e archi post-resolution vivono in src/deduplication_schema.py (ResolvedNode, ResolvedEdge, ResolvedGraph, Stage6Proposal), DEDUP_SCHEMA_VERSION = "0.1.1". Separato da schema.py perché provenances: list[Provenance], merged_from, merge_method esistono solo dopo la resolution. Gli archi risolti portano `source_type` e `target_type` (derivati dai nodi, vedi ADR-021).
Esplicitamente `NON` incluso:

merge di Theme near-duplicati (bellezza vs bellezza_e_virtu): non è duplicazione ma struttura tematica fine, passa allo stadio 5 enrich.
generazione di archi EMBODIES: solo proposte tracciate per lo stadio 5 enrich.
health_checkup con review umana su `anomalies.json`: sì, supportato (vedi ADR-022).
riscrittura di description o name.
caricamento su Neo4j (è lo stadio 3.5 a valle, ora consuma resolved_graph.json).


---



## Pattern `health_checkup` (checkpoint di fine stadio)

**Scopo**: analisi deterministica post-stadio — metriche, anomalie, log. **Non modifica** il grafo. Zero LLM. Idempotente.

**Output standard** (per ogni stadio che lo implementa):
- `dashboard.html` — verdetto, guida controlli, tabella problemi/soluzioni (aprire per primo)
- `checks.json` — ogni controllo con `status` (`pass` | `warn` | `fail` | `info`), perché, cosa verificare, soluzioni
- `metrics.json` — conteggi, ratio, distribuzioni
- `review_queue.json` — solo item per review umana (hub, review_needed)
- `health_log.json` — `verdict` (`pass` | `pass_with_warnings` | `fail`), versioni, parametri

**Verdetto**: `pass` = stadio convalidato, si procede; `pass_with_warnings` = OK con review consigliata; `fail` = bloccare (controlli con `blocks_stage`).

**Review umana**: opzionale, supportata da `review_queue.json` + dashboard. Usare `chunks.json` per il testo. Non è uno stadio numerato separato.

**Implementato**: `stage_3-3_health_checkup.py` → `data/stage_3/3_health_checkup/`; `stage_4-5_health_checkup.py` v0.2.0 → `data/stage_4/5_health_checkup/`; `stage_5-6_health_checkup.py` v0.1.0 → `data/stage_5/6_health_checkup/` (post-enrich, ADR-028); `stage_6-2_health_checkup.py` v0.1.0 → `data/stage_6/2_health_checkup/` (post-index, ADR-029: copertura nodi + smoke retrieval).

**Note**: Stadio 3 `extraction_analysis.py` resta equivalente informale.

---

## Stadio 5 — Enrich (ex stadio 6)

**Scopo**: arricchimento semantico **cross-chunk** sul grafo deduplicato: nodi e archi che l'estrazione mono-chunk non può produrre.

**Stato**: fatto e validato. Schema bumpato come da ADR-023 (`SCHEMA_VERSION 0.3.0` con `SPECIALIZES`; `DEDUP_SCHEMA_VERSION 0.2.0` con `is_macro`). Nessuna ri-estrazione: l'output dello stadio 3 resta byte-identico.

**Input**: `resolved_graph.json` + `resolver_log.json` (`stage6_proposals`) dello stadio 4; `enriched_graph.json` a catena fra i sotto-stadi.

**Architettura ricorrente** (ADR-024): ogni sotto-stadio è un anello che legge un `enriched_graph.json` (shape `ResolvedGraph`, provenienze inline) e ne scrive uno nuovo + una *mappa di decisioni* ispezionabile. I sotto-stadi che inferiscono semantica seguono il pattern **candidati deterministici → giudice LLM locale → resolver/build**: embeddings BGE-M3 per il recall, Qwen3-14B via LM Studio (structured output, cache idempotente) per la precisione. Le derivazioni puramente deterministiche NON usano LLM.

**Sotto-stadi** (output in `data/stage_5/<n>_<nome>/enriched_graph.json` + mappe):
- **5-1 `embodies`** — deterministico. Materializza gli archi `EMBODIES` dalle `Stage6Proposal` dello stadio 4. Risultato: 4 proposte → 3 archi `(Event→Theme)`, 1 saltata perché `Phase→Theme` non è ammesso da `EDGE_COMPATIBILITY` (punto aperto ADR-023).
- **5-2 `themes`** — consolidamento dei Theme `same` (ADR-023): `5-2a` candidati (coseno BGE-M3 + Jaccard lessicale), `5-2b` giudizio LLM a 3 vie su evidence_span (`same`/`refinement`/`distinct`), `5-2c` resolver per componenti connesse con soglia 0.97 e intercettazione dei conflitti `same×refinement`. Risultato: 307 Theme; 8 `same`, 17 `refinement`; 1 cluster fuso in silenzio + 1 promosso a mano, 4 cluster in review, 1 conflitto (`anima_e_corpo`/`corpo_e_anima`/`corpo_e_spirito`) non fuso. Nodi 2460 → 2458.
- **5-3 `hierarchy`** — gerarchia tematica in quattro passi (ADR-025): `5-3a` seed DAG dai refinement + clustering (97 cluster, 34 singoletti), `5-3b` giudizio dei cappelli per cluster (Qwen), `5-3c` aggancio dei 95 orfani ai 71 cappelli, `5-3d` build deterministico che posa gli archi `SPECIALIZES` e i flag `is_macro`. Risultato: **208 `SPECIALIZES`**, 41 cappelli promossi + 30 sintetizzati (71 macro-temi).
- **5-4 `echoes`** — archi sciolti `ECHOES` Event→Event fra scene lontane (ADR-026): candidati per coseno sulla description con esclusione delle coppie adiacenti (`min_chunk_gap`), giudizio Qwen con bias conservativo. Risultato: 665 Event, 122 coppie candidate → **8 `ECHOES`**.
- **5-5 `transforms`** — archi sciolti `TRANSFORMS_INTO` Phase→Phase (ADR-026): Phase consecutive nella stessa Era (ordine per chunk medio), giudizio Qwen con bias conservativo. Person→Person fuori scope (dopo dedup il soggetto è un solo nodo); Era→Era già fatto in 3.5. Risultato: 66 Phase, 62 coppie → **14 `TRANSFORMS_INTO`**.
- **5-6 `health_checkup`** — checkpoint deterministico post-enrich (ADR-028, pattern ADR-022): output in `data/stage_5/6_health_checkup/` (`dashboard.html`, `checks.json`, `metrics.json`, `review_queue.json`, `health_log.json`). Verdetto sul run Adriano: **`pass_with_warnings`** (warn su decisioni Theme pendenti e hub da spot-check; 0 fail). Contributo enrich misurato: **233 archi aggiunti** (3 EMBODIES + 208 SPECIALIZES + 8 ECHOES + 14 TRANSFORMS_INTO).

**Grafo finale** (output 5-5): 2488 nodi (di cui 71 macro-temi), 4691 archi.

**Cosa NON fa**: deduplica strutturale (stadio 4), indicizzazione RAG (stadio 6).

**Validazione**: ogni sotto-stadio ricostruisce e valida `ResolvedGraph` (integrità referenziale + `EDGE_COMPATIBILITY`) alla scrittura; il `5-6 health_checkup` certifica il grafo finale. Resta da fare la **review umana** sul grafo arricchito (coda in `review_queue.json`: 65 item — artefatti enrich + hub) prima dell'indice.

**Punti aperti** (vedi ADR-027): conflict cluster `anima/corpo` non fuso; `EMBODIES` con sorgente `Phase` saltato; review dei 4 cluster `theme` sotto soglia.

---

## Stadio 6 — Index (ex stadio 7)

**Scopo**: costruzione indice RAG ibrido (vettoriale + grafo) per l'agente conversazionale in prima persona. È la **frontiera fra pipeline di processing (0→5) e inferenza**: tutto ciò che è costruzione di indici vive qui; la cartella `inference/` si limita a consumarli (ADR-029).

**Stato**: in corso. Build e health_checkup implementati (v0.1.0). Resta aperta la review umana post-enrich e l'eventuale indicizzazione semantica dei nodi/temi (vedi punti aperti).

**Input**: `data/stage_2/chunks.json` (testo) + `data/stage_5/5_transforms/enriched_graph.json` (grafo arricchito). La review umana post-enrich è raccomandata prima dell'indice definitivo, ma non blocca il prototipo.

**Sotto-stadi**:
- **6-1 `index`** (`src/stage_6-1_index.py`, `STAGE_VERSION 0.1.0`) — deterministico, zero LLM. Embedda i 310 chunk con BGE-M3 (stesso embedder dello stadio 5, `normalize_embeddings=True`) e scrive in `data/stage_6/1_index/`: `vectors.npy` (matrice (N,D) float32), `meta.json` (record per chunk: part, token_count), `chunk_texts.json` (testi in RAM lato inferenza), `manifest.json` e `index_log.json`. Il **grafo NON è duplicato**: il `manifest.json` registra la provenienza (path + `sha256` + versioni + conteggi) del grafo contro cui l'indice è costruito; l'inferenza legge il grafo da `stage_5/` e valida la coerenza col manifest (ADR-029).
- **6-2 `health_checkup`** (`src/stage_6-2_health_checkup.py`, `STAGE_VERSION 0.1.0`) — checkpoint deterministico (pattern ADR-022/028) → `data/stage_6/2_health_checkup/` (`dashboard.html`, `checks.json`, `metrics.json`, `review_queue.json`, `health_log.json`). Check: artefatti presenti e coerenti (blocca), allineamento indice↔chunk e indice↔grafo via hash del manifest, **copertura nodi** (% di nodi con ≥1 chunk di provenienza indicizzato; Era e nodi sintetici scoperti sono attesi), chunk indicizzati senza nodi, **smoke retrieval** (5 domande fisse → ≥1 chunk e ≥1 nodo, senza LLM; `--no-smoke` per saltarlo).

**Consumo lato inferenza**: `inference/config.yaml` punta `index_dir` a `../Adriano_graph/data/stage_6/1_index`; `inference/rag/index.py` fa solo `load`+`search`; `session.py` legge il `manifest.json` e avvisa se il grafo è cambiato dopo la build.

**Punti aperti**:
- review umana post-enrich (coda di 65 item in `stage_5/6_health_checkup/review_queue.json`) prima dell'indice definitivo.
- indicizzazione semantica dei **nodi/temi** del grafo: oggi l'indice è sui soli chunk, quindi macro-temi e gerarchia `SPECIALIZES` sono raggiungibili solo via chunk. Eventuale `stage_6-3` (embedding dei Theme/macro-temi) per un ibrido pieno.

---

## Struttura file progetto (aggiornata)

```
storie/adriano_graph/
├── data/
│   ├── memorie_adriano.pdf              (sorgente)
│   ├── dataset_qa.json                  (~1200 Q&A, per valutazione futura)
│   ├── stage_0/
│   │   ├── raw_text.txt                 ✓ fatto
│   │   ├── structure.json               ✓ fatto (offset delle 6 parti)
│   │   ├── extraction_log.json          ✓ fatto
│   │   └── intermediates/               (snapshot di debug)
│   ├── stage_1/
│   │   ├── cleaned_text.txt             ✓ fatto (copia di raw_text per ora)
│   │   ├── cleaning_log.json            ✓ fatto
│   │   └── inspection_report.json       ✓ fatto
│   ├── stage_2/
│   │   ├── chunks.json                  ✓ fatto (310 chunk)
│   │   └── chunking_log.json            ✓ fatto
│   ├── stage_3/full_runs/<datetime>
│   │                       └── extracted_graph.json ✓ fatto
│   ├── stage_4/3_resolve/resolved_graph.json        ✓ fatto
│   ├── stage_5/5_transforms/enriched_graph.json     ✓ fatto (grafo arricchito)
│   └── stage_6/
│       ├── 1_index/                     ✓ fatto (vectors.npy, meta.json, chunk_texts.json, manifest.json)
│       └── 2_health_checkup/            ✓ fatto (dashboard.html, checks.json, ...)
├── config/
│   └── cleaning_rules.yaml              ✓ fatto (tutte disabled per Yourcenar)
│   (gli esempi few-shot di stadio 3 vivono in data/stage_3/few_shots/,
│    ADR-011; il prompt vive in src/stage_3_prompt.py, ADR-012)
├── src/
│   ├── schema.py                        ✓ fatto (puro strutturale, ADR-012)
│   ├── stage_0_extract_pdf.py           ✓ fatto (v0.2.0)
│   ├── stage_1_clean.py                 ✓ fatto (v0.1.0)
│   ├── stage_2_chunk.py                 ✓ fatto (v0.1.0)
│   ├── stage_3-1_prompt.py              ✓ fatto (contratto semantico, PROMPT_VERSION 0.3.0)
│   ├── stage_3-2_extract.py             ✓ fatto (runner, STAGE_VERSION 0.1.0)
│   ├── stage_3-3_health_checkup.py      ✓ fatto (v0.1.0)
│   ├── stage_4-3_resolve.py             ✓ fatto (fonte di verità: resolved_graph.json)
│   ├── stage_4-4_structure.py           ✓ fatto (opzionale — export snelli per esplorazione)
│   ├── stage_4-5_health_checkup.py      ✓ fatto (v0.2.0)
│   ├── stage_5-1_embodies.py            ✓ fatto (EMBODIES deterministici)
│   ├── stage_5-2a_theme_candidates.py   ✓ fatto (candidati merge Theme, BGE-M3 + Jaccard)
│   ├── stage_5-2b_theme_judge.py        ✓ fatto (giudizio 3-vie, Qwen3-14B)
│   ├── stage_5-2c_theme_resolve.py      ✓ fatto (resolver dei `same`)
│   ├── stage_5-3a_hierarchy.py          ✓ fatto (seed DAG + clustering)
│   ├── stage_5-3b_hierarchy_judge.py    ✓ fatto (cappelli per cluster, Qwen)
│   ├── stage_5-3c_orphan_assign.py      ✓ fatto (aggancio orfani ai cappelli)
│   ├── stage_5-3d_hierarchy_build.py    ✓ fatto (posa SPECIALIZES + is_macro)
│   ├── stage_5-4_echoes.py              ✓ fatto (ECHOES Event→Event)
│   ├── stage_5-5_transforms.py          ✓ fatto (TRANSFORMS_INTO Phase→Phase)
│   ├── stage_5-6_health_checkup.py      ✓ fatto (v0.1.0, checkpoint post-enrich)
│   ├── stage_6-1_index.py               ✓ fatto (v0.1.0, build indice RAG ibrido + manifest)
│   ├── stage_6-2_health_checkup.py      ✓ fatto (v0.1.0, copertura nodi + smoke retrieval)
│   └── ...
├── notebooks/
│   └── 00_inspect_source.ipynb          (ricognizione PDF)
├── PIPELINE.md                          (questo file)
├── .env                                 (ANTHROPIC_API_KEY, NEO4J_*)
└── ../environment.yml   (root repo — env unificato)
```

---

## Decisioni di design (ADR)

Ogni decisione di design importante va qui, datata, formato breve.
Quando una decisione viene rivista, NON cancellare la vecchia: aggiungere una nuova voce che la supersede e marcare quella vecchia come `[SUPERSEDED da ADR-NNN]`.

---

### ADR-001 — `narrative_phase` non viene assegnato in stadio di chunking
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Si è valutato se annotare ogni chunk con una "fase narrativa di vita dominante" già in stadio 2, oppure lasciarla emergere come entità Phase nello stadio 3.
**Decisione**: Lasciar emergere come entità in stadio 3.
**Razionale**: Assegnare in stadio 2 mescolerebbe due stadi (chunking + estrazione semantica), violando il principio di modularità. Le Phase sono entità del grafo a tutti gli effetti, non metadati di chunk.
**Conseguenze**: Lo stadio 2 resta puramente strutturale/deterministico. Lo stadio 3 dovrà gestire Phase come tipo di nodo (già nello schema).

---

### ADR-002 — Chunking strutturale di base, non semantico
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Tre opzioni valutate per il chunking: (A) strutturale paragrafo + finestra token, (B) semantico via embedding (es. SemanticSplitterNodeParser), (C) ibrido con LLM che propone i confini.
**Decisione**: Opzione A. Paragrafo come unità base, aggregazione con vincoli min/max token, confini duri sulle sei parti.
**Razionale**: Deterministico, idempotente per costruzione, zero costi LLM, ispezionabile. La semantica di Yourcenar è associativa e l'embedding tende a spezzare proprio dove l'autore vuole tenere insieme. Si passa a B/C solo se gli esperimenti di estrazione mostrano perdita misurabile di legami `REFLECTS_ON` o `ECHOES`.
**Conseguenze**: Lo stadio 2 rimane senza dipendenze LLM e quindi più veloce e replicabile. Possibile ri-chunking semantico più avanti come stadio opzionale.

---

### ADR-003 — Sorgente PDF, non EPUB
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: L'EPUB della traduzione Storoni Mazzolani ha un unico `<p>` per l'intero testo (paragrafi persi alla radice) e una conversione precedente a txt ha introdotto errori sistematici tipo "Il" → "11". Il PDF della stessa edizione è text-based, ha paragrafi conservati, titoli delle parti separati e in maiuscolo, nessun numero di pagina, niente errori di battitura visibili.
**Decisione**: Usare il PDF come sorgente unica. Aggiungere uno `stage_0_extract_pdf` davanti alla pipeline.
**Razionale**: Risolve alla radice sia il problema dei paragrafi sia quello degli errori OCR-like. Lo stadio 0 isola tutta la complessità formato-specifica in un punto solo, mantenendo gli stadi successivi puliti e riusabili (in produzione basterà sostituire stadio 0 con `stage_0_transcribe`: audio → trascrizione).
**Conseguenze**: Serve scrivere uno stadio 0 robusto e ispezionabile. Lo stadio 1 (cleaning) probabilmente avrà un dizionario quasi vuoto per Yourcenar, ma resta come slot architetturale.

---

### ADR-004 — Cleaning trasparente a regole, non LLM-assistito
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Per correggere eventuali errori residui di estrazione si poteva scegliere tra (1) regole regex deterministiche, (2) un passaggio LLM che propone correzioni, (3) niente cleaning esplicito.
**Decisione**: Opzione 1, con dizionario in `config/cleaning_rules.yaml` e log dettagliato di ogni sostituzione.
**Razionale**: Deterministico, ispezionabile, idempotente, allineato al requisito di tracciabilità non-negoziabile del prodotto biografico. Un LLM rischia di parafrasare in silenzio, e per un testo letterario ogni alterazione semantica è grave. Lo stesso slot servirà per le trascrizioni da oral history, dove il dizionario sarà domain-specific (nomi propri, toponimi, termini ricorrenti mal trascritti).
**Conseguenze**: Compilazione del dizionario incrementale, basata su anomalie osservate. Per Yourcenar probabilmente quasi vuoto.

---

### ADR-005 — Paragrafi rilevati per indentazione, non per riga vuota
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: La prima versione dello stadio 0 (v0.1.x) identificava i confini di paragrafo solo tramite riga vuota nel PDF. Funziona su PDF con interlinea ampia tra paragrafi, fallisce sull'edizione Storoni Mazzolani che usa rientro tipografico classico. Risultato iniziale: 12 "paragrafi" giganti su 521k caratteri.
**Decisione**: Estendere lo stadio 0 (v0.2.0) a leggere le coordinate `x0` delle prime parole di riga via `pdfplumber.extract_words`, calcolare la mediana per pagina, marcare come inizio paragrafo le righe la cui prima parola è indentata oltre soglia (`mediana + 5pt`). Mantenere la riga vuota come secondo segnale (OR logico).
**Razionale**: La struttura del documento sorgente va preservata dove esiste, non ricostruita a valle con euristiche post-hoc su punteggiatura. Risultato: 322 paragrafi su 170 pagine processate, ~1.9 per pagina, coerente con il layout visivo del PDF.
**Conseguenze**: Stadio 0 a v0.2.0. Stadio 1 va rilanciato sul nuovo `raw_text.txt`. Formato dei file di output invariato (solo `paragraph_detection_method` aggiunto come metadato in `structure.json`). 
**Lezione metodologica**: trasferibile alle biografie da oral history (analoghi: pause vocali, esitazioni, cambi di tono — informazioni che esistono nel segnale e che il primo stadio deve catturare, non far perdere e poi inseguire con regex).

---

### ADR-006 — Chunking 1-paragrafo-1-chunk con accorpamento dei soli paragrafi cortissimi
**Data**: 2026-05-13
**Stato**: attivo (supersede parzialmente ADR-002 sui parametri concreti)
**Contesto**: ADR-002 prevedeva chunking strutturale con vincoli min ~200 / max ~800 token, basato sull'ipotesi di paragrafi brevi (50-70 token) da accorpare. Dopo recupero corretto dei paragrafi (ADR-005), si osserva che la media reale è ~400-500 token per paragrafo. L'accorpamento perde significato per la maggioranza dei casi.
**Decisione**: Strategia "1 chunk = 1 paragrafo" come regola di base, con un'unica eccezione: paragrafi sotto soglia minima (80 token) vengono accorpati al paragrafo successivo (o al precedente se sono l'ultimo della parte). Nessuna soglia massima: paragrafi lunghi restano interi (rispetto dell'unità retorica autoriale). Confini duri sulle sei parti preservati.
**Razionale**: Massima granularità della provenienza (un nodo del grafo proviene da esattamente un paragrafo identificabile), massimo rispetto della struttura autoriale. La soglia minima di 80 token cattura i rari paragrafi monofrase senza introdurre complessità. Allineato alle biografie da oral history: turni di parlato lunghi/brevi alterneranno, e l'accorpamento dei turni cortissimi è lo stesso problema.
**Conseguenze**: Stadio 2 più semplice del previsto. Niente sliding window, niente overlap. Strategia rivedibile se l'estrazione a valle mostra che paragrafi-monstre (>1500 token) degradano la qualità.

---

### ADR-007 — Estrazione con prompt diretto a Claude, NO LlamaIndex
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Lo stack iniziale prevedeva LlamaIndex `SchemaLLMPathExtractor` come estrattore di stadio 3. Suggerimento generico, fatto prima di aver consolidato schema, lingua e requisiti di provenienza. Valutate le opzioni reali: (A) `SchemaLLMPathExtractor` con override del prompt template; (B) prompt diretto a Claude via SDK ufficiale `anthropic` + validazione Pydantic.
**Decisione**: Opzione B. Funzione `extract_from_chunk` scritta a mano (~100 righe), nessun framework di estrazione.
**Razionale**:
- Il prompt template di LlamaIndex è in inglese e parametrizzato. Per usare le `EXTRACTION_INSTRUCTIONS` italiane di `schema.py` bisognerebbe sovrascriverlo, neutralizzando metà del valore del framework.
- La distinzione Event/Reflection di Yourcenar è una sfumatura enunciativa che richiede prompting molto specifico, non un'astrazione generale "entità tipizzate".
- La provenienza arricchita (chunk_id, model, timestamp, confidence, evidence_span, human_validated) è disegnata su misura per il requisito di tracciabilità del prodotto biografico. Costruirla sopra `Node.metadata` di LlamaIndex è un combattimento contro l'astrazione.
- LlamaIndex evolve rapidamente. Dipendere dal framework per la parte centrale (estrazione) della pipeline che deve girare in produzione è un rischio.
- Quando arriveremo allo stadio 7 (RAG ibrido), LlamaIndex resta candidata per *quella parte*, importando il JSON intermedio. Da rivalutare allora.
**Conseguenze**: codice di estrazione tutto sotto controllo, dipendenze ridotte (solo `anthropic` e `pydantic`), prompt italiano nativo. Sostituibilità futura (modelli locali via Ollama/vLLM) diventa banale: cambia solo la funzione di chiamata.

---

### ADR-008 — Few-shot prompting con esempi versionati, NO zero-shot
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Per l'estrazione si poteva scegliere tra zero-shot (solo istruzioni + schema), few-shot (istruzioni + esempi reali), o pre-elaborazione in due passi (LLM riassume → LLM estrae).
**Decisione**: Few-shot con 3-5 esempi annotati a mano su chunk veri del libro, conservati in `config/extraction_examples.yaml`. Versionati: quando cambiano, si bumpa la versione del prompt di estrazione e si ri-estrae.
**Razionale**:
- Gli esempi fungono da "contratto" canonico di cosa è Event e cosa è Reflection nel progetto. Sono la documentazione su cui costruire fiducia con chi valida la biografia (team interno, familiari del cliente): più trasparente di "il modello è bravo".
- Migliorano la qualità su modelli grandi e diventano *indispensabili* su modelli locali piccoli (7-8B parametri) che potrebbero essere usati in produzione per ragioni di privacy/costo. Il prompt funge da training in-context.
- Disaccoppiamento dominio/schema: lo schema resta invariato, gli esempi diventano "configurazione di dominio" (per Yourcenar e poi per le biografie cliente due insiemi diversi).
**Conseguenze**: lavoro intellettuale di scrittura esempi su chunk veri, da fare in conversazione, non delegato all'agente coder. Gli esempi vivono in repo separato dal codice.

---

### ADR-009 — Stadio 3 produce JSON intermedio, NO scrittura diretta su Neo4j
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: L'estrazione poteva (a) scrivere direttamente su Neo4j durante l'iterazione sui chunk, oppure (b) produrre un JSON intermedio `extracted_graph.json` da caricare in uno stadio separato.
**Decisione**: Opzione (b). Stadio 3 = estrazione → JSON. Stadio 3.5 = caricamento JSON → Neo4j.
**Razionale**:
- L'estrazione è costosa (chiamate API). Se il caricamento Neo4j ha bug, con (a) ri-spendi tutto. Con (b) rilanci solo il caricamento.
- Il JSON intermedio è ispezionabile come testo, diffabile in git, facile da validare a mano e da convertire in altri formati.
- Disaccoppia il dominio "estrazione" dal dominio "persistenza grafo". Future migrazioni di storage (es. da Neo4j ad altro) toccano solo lo stadio 3.5.
- Allineato al principio di idempotenza per stadio: ogni stadio produce file ispezionabili come prodotto principale.
**Conseguenze**: si aggiunge uno stadio 3.5 alla pipeline. Nessuno svantaggio operativo concreto.

---
### ADR-010 — Stadio 3 Utilizzo Sonnet 4.6 per extraction
**Data**: 2026-05-14
**Stato**: attivo
**Contesto**: Mancava la decisione aggiornata su a quale modello affidare l'extraction nei primi test
**Decisione**: scelta di Sonnet 4.6 come modello default con confronto Opus 4.7 sui test
**Razionale**:
- Miglior bilanciamento costo/performance 
- chiavi e dipendenze già integrate nel progetto
**Conseguenze**: servizio affidabile, non il più economico ma buon punto di partenza.

---
### ADR-011 — esempi few-shot scritti a mano dall'utente, non delegati
**Data**: 2026-05-14
**Stato**: attivo
**Contesto**: le estrazini di riferimento potevajnoessere prodotti in diversi modi.
**Decisione**: Gli esempi few-shot e di test sono stati prodotti a mano da chucks casuali, e confrontati con Opus4.7
- gli esempi di test e few-shots sono stati spostati in  `data/stage_3/few_shots/` e `data/stage_3/test/`
- `data/stage_3/notebooks/` contiene le estrazioni fatte ma non idonee a rappresentare test o few-shots
**Razionale**:
- Necessità di imparare i principi e lo svolgimento dell'annotazione semantica
**Conseguenze**: tempo più lungo, forse imprecisioni ma migliore comprensione del processo

---

### ADR-012 — Separazione netta schema strutturale / contratto semantico, e formato "flat" del tool
**Data**: 2026-05-14
**Stato**: attivo
**Contesto**: Lo stadio 3 ha due responsabilità intrecciate ma distinte: (1) lo *schema* dei dati estratti (shape di nodi/archi, tipi ammessi, regole di compatibilità) e (2) il *contratto semantico* col modello (descrizione in italiano dei tipi di nodo, distinzione Event/Reflection, esempi few-shot, regole operative del prompt). Nella v0 di `src/schema.py` queste due cose coabitavano: `EXTRACTION_INSTRUCTIONS` era una stringa di prompt dentro il modulo schema, contaminata da riferimenti dominio-specifici ("Memorie di Adriano", "Adriano-narratore", "Storoni Mazzolani"). Inoltre lo scheletro di `EXTRACTION_TOOL` chiedeva nodi/archi in formato "flat" (`confidence` ed `evidence_span` top-level), mentre il `Node` Pydantic li teneva annidati dentro `Provenance`. Discrepanza da risolvere consapevolmente.
**Decisione**:
1. **`schema.py` resta puro strutturale**: enum (`NodeType`, `EdgeType`), `EDGE_COMPATIBILITY`, modelli Pydantic (`Provenance`, `Node`, `Edge`, `ExtractedGraph`), `is_edge_valid`. Nessun testo di prompt, nessun riferimento al dominio. Modifiche a `schema.py` = bump di `SCHEMA_VERSION`.
2. **`stage_3_prompt.py` ospita l'intero contratto semantico**: `SYSTEM_PROMPT` (testo che era in `EXTRACTION_INSTRUCTIONS`, adattato per tool-use e formato flat), `EXTRACTION_TOOL` (JSON schema del tool), `FEW_SHOT_EXAMPLES` (caricati e trasformati a runtime dai file in `data/stage_3/few_shots/`), `build_messages` e `build_request_payload`. Ha un suo `PROMPT_VERSION` indipendente.
3. **Formato di output del modello "flat"**: il tool `submit_extraction` riceve nodi e archi con `confidence` ed `evidence_span` top-level, NON dentro `provenance`. I campi tecnici di provenance (`model`, `timestamp`, `schema_version`, `human_validated`, copia di `chunk_id`) sono arricchiti dal *chiamante* (`stage_3_extract.py`) al momento del wrapping in `Node`/`Edge` Pydantic. Il modello non sa nulla di questi campi e non li produce.
4. **Esempi few-shot caricati a runtime**: i file `data/stage_3/few_shots/ch_*.json` sono annotati a mano nel formato "fat" del knowledge graph finale (con `Provenance` annidata). La funzione `load_few_shot_examples()` in `stage_3_prompt.py` fa il bridge fat→flat al primo import. Il `chunk_id` reale è derivato dal nome del file (`ch_0047.json` → `ch_0047`); il `chunk_text` viene recuperato da `data/stage_2/chunks.json`. I file restano la fonte di verità unica, il modulo Python non si gonfia.
**Razionale**:
- Il prototipo dovrà girare anche su altri testi (biografie cliente). Per cambiare dominio servirà sostituire `stage_3_prompt.py` (e gli esempi in `data/stage_3/few_shots/`), non riscrivere lo schema. Tenerli separati rende la portabilità un cambio di modulo, non un refactor strutturale.
- `SCHEMA_VERSION` e `PROMPT_VERSION` ora hanno semantiche distinte e usabili: un'iterazione sulle istruzioni del prompt non invalida i dati estratti rispetto allo schema (basta bumpare `PROMPT_VERSION` per tracciare la differenza nei run).
- Il formato flat del tool minimizza i campi richiesti al modello: il modello produce solo informazione semantica (cosa estrae e perché), il sistema aggiunge automaticamente i metadati tecnici. Riduce token, semplifica l'output e isola le responsabilità.
- I file few-shot in formato fat sono leggibili come prodotti finiti dell'annotazione (la stessa shape che il grafo finale avrà), utili per validazione manuale e ispezione. Convertirli in flat in memoria non costa nulla.
**Conseguenze**: `EXTRACTION_INSTRUCTIONS` rimosso da `schema.py`. `stage_3_prompt.py` esistente popolato con `SYSTEM_PROMPT` riscritto (mantiene il taglio Yourcenar, sostituisce la chiusa "output JSON valido" con "invoca `submit_extraction`"), `FEW_SHOT_EXAMPLES` derivato da disco. `stage_3_extract.py` (prossimo) dovrà ricostruire `Provenance` a partire da `chunk_id` runtime + `model` + `timestamp` + `SCHEMA_VERSION` + i `confidence`/`evidence_span` flat ricevuti dal modello, prima di costruire `Node`/`Edge` Pydantic. La citazione "EXTRACTION_INSTRUCTIONS di schema.py" in ADR-007 resta come riferimento storico, superseduta sul punto da questo ADR.

---

### ADR-013 — Prompt caching a due breakpoint sul prefisso stabile
**Data**: 2026-05-14
**Stato**: attivo
**Contesto**: Misurazione con `tiktoken` (cl100k_base) sul payload di stadio 3 dopo l'introduzione di few-shot e tool: SYSTEM_PROMPT ~1.748 tok, tool schema ~461 tok, 4 esempi few-shot ~11.813 tok, totale prompt fisso ~14.000 tok ad ogni chiamata, contro un `chunk_text` corrente di ~500-1.000 tok. Cioè il 95% del costo per chunk è prompt fisso che NON cambia attraverso i 310 chunk. Valutate altre leve per ridurre token (troncare le `description` nei few-shot, rimuovere quelle degli archi, scendere a 3 esempi, comprimere il SYSTEM_PROMPT) ma tutte trade-offano qualità semantica contro token, e la qualità è prioritaria, soprattutto in vista dell'uso futuro su modelli più piccoli (Sonnet basic, 7-8B locali in produzione) in cui esempi ricchi di `description` sono didatticamente più importanti, non meno.
**Decisione**: Sfruttare il prompt caching di Anthropic (`cache_control: ephemeral`, TTL ~5 minuti) con **due breakpoint** sul prefisso stabile:
1. Sul blocco `text` del `SYSTEM_PROMPT` nel parametro `system`.
2. Sul `tool_result` content block dell'ultimo (4°) few-shot dentro `messages`.
La sola parte non cachata della chiamata è il `chunk_user_message` finale, che cambia per ogni chunk. Nessuna riduzione del contenuto semantico: gli esempi restano integri, le `description` restano lunghe, i 4 esempi restano 4.
**Razionale**:
- Il guadagno è massimo dove il costo era massimo (i ~12k token di few-shot stabili). Spostare il breakpoint dopo gli esempi rende cachabile tutto ciò che resta uguale attraverso i 310 chunk.
- Due breakpoint anziché uno costano un solo cache write extra alla prima chiamata, ma permettono granularità durante l'iterazione di prompt design: cambiare solo il SYSTEM_PROMPT invalida il primo breakpoint; cambiare un esempio invalida solo il secondo.
- TTL ephemeral (5 min) è adatto a una run sequenziale sui 310 chunk: ogni chiamata rinfresca la cache. Una run interrotta e ripresa dopo >5 min ripagherà il write una volta.
- Soglia minima Anthropic di 1024 tok per blocco cachable rispettata da entrambi i breakpoint (1748 e ~12.000 tok).
- Zero impatto su qualità semantica per il modello: il modello vede lo stesso prompt identico, cambia solo come viene fatturato.
**Conseguenze**:
- `_format_user_tool_result(tool_use_id, with_cache=False)` accetta un flag per piazzare `cache_control` solo sull'ultimo few-shot. `build_messages` lo attiva per `i == last_idx`.
- Stima costi su 310 chunk (Sonnet 4.6, ~$3/Mtok input): da ~$13 senza cache a ~$2 con cache (write una volta + 309 read a ~10%), ~85% di risparmio. Numeri da verificare quando partirà il primo run vero.
- `PROMPT_VERSION` resta `0.1.0`: il contenuto del prompt non è cambiato, solo come viene fatturato.
- Documentato negli `# Prompt caching` comments di `stage_3-1_prompt.py` per facilitare la lettura.
- Aggiunta una **leva opzionale** `REMOVE_DESCRIPTION` (costante module-level in `stage_3-1_prompt.py`, default `False`) che, se alzata a `True`, omette le `description` di nodi e archi dal formato flat dei few-shot. Risparmio stimato ~3-4k token, costo potenziale qualità sui modelli più piccoli (dove le `description` fungono da "training in context" del tono descrittivo). Da abilitare solo dopo aver verificato che la qualità su un primo run con `False` regge anche senza. I file `data/stage_3/few_shots/*.json` restano comunque integri: il taglio avviene solo in memoria in `_flatten_node` / `_flatten_edge`.

---

### ADR-014 — Raffinamento del prompt di estrazione (PROMPT_VERSION 0.2.0)
**Data**: 2026-05-17
**Stato**: attivo
**Contesto**: dopo il primo round qualitativo sul prompt v0.1.0 (ispezione su Pila B + revisione manuale) sono emerse tre debolezze ricorrenti: (a) frammentazione eccessiva delle scene in micro-Event, con un nodo per ogni gesto e stato emotivo della scena; (b) Theme estratti solo quando lessicalizzati, con perdita di temi chiaramente incarnati ma non nominati; (c) uso libero di `CONTRASTS_WITH` per opposizioni desunte e non marcate dal testo; (d) antefatti narrativi richiamati di sfuggita in subordinata trattati come Event autonomi. Il SYSTEM_PROMPT in `stage_3-1_prompt.py` è stato rivisto per correggere questi pattern.
**Decisione**: bump di `PROMPT_VERSION` da `0.1.0` a `0.2.0`. Le modifiche concrete al SYSTEM_PROMPT sono:
1. **Event con grana "a scena, non a fotogramma"**: una scena è un'unità mnemonica narrativamente coesa, e i suoi dettagli interni (gesti, sguardi, schieramenti, stati fisici/emotivi del momento) vivono nella description del nodo scena, non in nodi separati. Esempio canonico nel prompt: traversata dell'Eufrate con pallore di Flegone, apprensione degli ufficiali, agio di Opramoas → un solo Event `incontro_diplomatico_eufrate`.
2. **Atto politico distinto dentro la scena**: dentro la stessa scena, un atto con conseguenze autonome (es. "restituzione_principessa") va estratto come Event a sé, non assorbito nella description della scena.
3. **Eccezione "contrasti espliciti"**: stati o comportamenti che il narratore mette uno accanto all'altro per opposizione marcata vanno estratti come Event distinti collegati da `CONTRASTS_WITH`. Soglia alta: contrasto presente nel testo, non desumibile dall'estrattore.
4. **Più scene = più Event collegati da ECHOES**: se un Event "composto" si articola su scene distinte, vanno estratte come Event separati con archi ECHOES.
5. **Antefatti in subordinata**: fatti del passato richiamati di sfuggita in una subordinata (es. "il trono che Traiano aveva portato via") NON diventano Event autonomi. Vivono nella description del nodo a cui si riferiscono. Emergeranno come Event quando un altro chunk li racconta per esteso. Demanda alla deduplicazione di stadio 4.
6. **Theme incarnato (oltre che nominato)**: Theme va estratto sia quando esplicitamente nominato, sia quando il paragrafo lo INCARNA attraverso scene e atti, anche senza lessicalizzarlo. **Test pratico**: se gli Event del paragrafo sembrano tutti orientati a illustrare una stessa idea astratta, quella idea è un Theme.
7. **`CONTRASTS_WITH` chiarito in elenco archi**: glossa esplicita che lo limita ai casi marcati dal testo.
8. **Regola operativa "Densità" (nuova, n.6)**: indicativa, non prescrittiva. Un paragrafo denso di Yourcenar produce tipicamente: una scena cardine + 1-2 atti distinti + attori + luogo + 1-2 temi + 1-2 Reflection. Dà al modello un'ancora di volumi attesi senza vincolarlo.
**Razionale**: la grana mnemonica (la scena come unità) è coerente con l'obiettivo finale del progetto. Un agente che fa parlare Adriano in prima persona deve poter recuperare *la scena* (con il suo grano di dettagli interni leggibili nella description) e non *i fotogrammi* (decine di micro-Event che frantumano la memoria episodica e producono recall confuso). Lo stesso vale per il Theme incarnato: la rete tematica di Yourcenar è spesso non lessicalizzata, e ignorarla significa amputare metà del valore conversazionale del grafo. Il vincolo "contrasto solo se marcato dal testo" e "antefatti restano in description" sono freni contro l'over-extraction speculativa, allineati al principio "sotto-estrai prima di sovra-estrarre".
**Conseguenze**:
- `src/stage_3-1_prompt.py`: `PROMPT_VERSION = "0.2.0"`. SYSTEM_PROMPT esteso (~5800 → ~7600 caratteri stimati, cresce di ~500 token; ancora ampiamente cachato dopo il primo hit, ADR-013 invariato).
- `SCHEMA_VERSION` resta `0.1.0`: la shape Pydantic dei dati estratti non è cambiata.
- `STAGE_VERSION` del runner `stage_3-2_extract.py` resta `0.1.0`: il comportamento del runner non è cambiato.
- I file annotati in `data/stage_3/few_shots/*.json` e `data/stage_3/test/*.json` vanno **rivisti a mano** per allineamento alle nuove regole. In particolare: deframmentare scene già spezzate in più Event, spostare antefatti dalle micro-Event alle description, valutare l'aggiunta di Theme incarnati. **Da fare prima del prossimo run** di estrazione, perché i few-shot incoerenti col prompt confondono il modello (specialmente su modelli piccoli).
- Il file `data/stage_3/extracted_graph_test.json` ha header `prompt_version: "0.1.0"`: da considerare archeologia. Archiviare o sovrascrivere al prossimo run.
- `resources/info/guida_annotazione_chunk.md` aggiornata in parallelo per riflettere le nuove regole (grana scena, Theme incarnato, contrasti espliciti, antefatti in subordinata, regola di densità tipica) e per registrare che il vincolo "token budget" sollevato nelle Cautele aperte è ora risolto dal caching (ADR-013).


### ADR-015 — Raffinamento del prompt di estrazione (PROMPT_VERSION 0.3.0)
**Data**: 2026-05-19
**Stato**: attivo
**Contesto**: durante la sessione di riannotazione dei few-shot a regola 0.2.0 (e dei chunk candidati a sostituire `ch_0127`, droppato perché insegnava al modello a estrarre Event da abitudini iterative) è emerso un fenomeno strutturale non coperto dalle regole correnti: nel testo di Yourcenar coesistono almeno tre modalità riflessive distinte, ma il prompt 0.2.0 le impacchetta tutte come Reflection. (1) Riflessione gnomica del narratore-oggi (presente generale, sentenza fuori dal tempo). (2) Giudizio retrospettivo del narratore-oggi su sé-allora come disposizione durativa ("ero stato sempre deciso a difendere le mie probabilità"). (3) Stato interiore di sé-allora rivissuto come accadimento situato ("una calma straordinaria era scesa su di me", "mi prese un capriccio", "improvvisamente mi tornò alla mente"). La modalità (3) è fenomenologicamente più vicina a un Event che a una Reflection: ha cambio di stato, datazione, soggetto, verbo puntuale. Trattandola come Reflection si perde la sua ancorabilità temporale e la possibilità di concatenarla a Event esterni in catene CAUSED/FOLLOWS — capacità centrale per una biografia-grafo che deve poter rispondere a domande del tipo "quando Adriano provò X per la prima volta?", "quali stati d'animo accompagnarono Y?", "come evolve la paura nel tempo?". Il caso emerso su `ch_0093` (la calma sopravvenuta dopo l'adozione) è il prototipo: tre criteri linguistici tutti presenti, ma in 0.2.0 finisce in Reflection e scompare come oggetto cercabile per tempo.
**Decisione**: bump di `PROMPT_VERSION` da `0.2.0` a `0.3.0`. Le modifiche concrete al SYSTEM_PROMPT sono:
1. **Event interno (quarta eccezione alla regola scena)**: lo stato d'animo rivissuto di sé-allora è Event interno, non Reflection. Tre criteri operativi, **tutti e tre** richiesti: (i) cambio di stato esplicito; (ii) ancoraggio temporale puntuale (esplicito o ricavabile); (iii) marca verbale puntuale (passato remoto, piuccheperfetto puntuale, passato prossimo come "scese", "mi accorsi", "sentii", "fui colto"). Imperfetti durativi e disposizioni abituali NON soddisfano (iii). Soglia alta, come per il contrasto esplicito: se manca anche uno solo dei tre, è Reflection o dettaglio in description.
2. **Aggancio obbligatorio dell'Event interno**: ogni Event interno DEVE avere almeno un arco verso un Event esterno o una Phase nel chunk stesso, scelto in quest'ordine: (1) `CAUSED` se il testo marca il nesso, anche solo via "dopo che/perché/ormai/grazie a" o per evidente implicazione narrativa, direzione canonica esterno → interno; (2) `FOLLOWS` se manca marca causale ma c'è successione temporale; (3) `DURING` verso una Phase se manca un Event esterno-ancora nel chunk ma la fase di vita è esplicita. Se non si trova alcun aggancio, l'Event interno NON va estratto: vivrà nella description dell'Event esterno pertinente.
3. **Tripartizione delle modalità riflessive nella sezione "Event vs Reflection"**: la sezione viene estesa nominando le tre modalità (1)-(2)-(3) e specificando che solo (1) e (2) sono Reflection, mentre (3) è Event interno. Avvertenza esplicita sul confine fra (2) e (3): in caso di dubbio scegliere (2) Reflection, perché (3) richiede che TUTTI E TRE i criteri siano evidenti.
4. **Calibrazione di densità sugli Event interni**: in un singolo chunk gli Event interni sono di norma 0, occasionalmente 1, raramente 2. Se ne emerge più di uno, rileggere: probabilmente uno è (2) giudizio retrospettivo travestito da (3).
5. **Aggiornamento del commento sulla composizione dei few-shot in `_FEW_SHOT_FILES`**: si registra il drop di `ch_0127` (insegnava abitudine come scena, incoerente con la regola scena 0.2.0 e ora con la 0.3.0) e l'ingresso di `ch_0092` (cremazione di Traiano: misto narrativo corale con contrasto esplicito Matidia-Plotina e atto politico-morale autonomo del patto del silenzio, copre pattern non altrimenti rappresentato). `ch_0047`, `ch_0113`, `ch_0122` mantenuti a 0.3.0 con estrazione invariata rispetto a 0.2.0 (zero criteri soddisfatti su nessuno stato: la regola Event interno non si attiva, prova di non-regressione). Il quarto few-shot dopo il refactor è `ch_0093` riannotato (la calma sopravvenuta dopo l'adozione: insegna esattamente il pattern Event interno della regola nuova).
**Razionale**: gli stati d'animo ricordati come accadimento situato sono un fenomeno frequente in biografia interiore e pesano in modo sproporzionato — sono i momenti pivot (lutto, presa di coscienza, capriccio improvviso, calma sopravvenuta) che l'agente conversazionale deve poter raccontare *come accaduti* e *quando*, non solo *commentati*. La scelta di promuoverli a Event interno (Opzione A) anziché creare un nuovo tipo di nodo dedicato `InnerState`/`Affect` (Opzione C) è guidata da due considerazioni: (i) reversibilità — la promozione a tipo dedicato resta possibile in futuro con bump di SCHEMA_VERSION; partire da un tipo dedicato e tornare a Event sarebbe più costoso (perdita di nodi); (ii) integrazione nativa nelle catene CAUSED/FOLLOWS con Event esterni, che è esattamente il pattern di interrogazione che giustifica la distinzione. Il rischio noto di A è la contaminazione fra (2) e (3): se la regola dei tre criteri non tiene, (2) entra di nascosto come Event interno e gonfia il tipo Event. La calibrazione di densità (≤1-2 Event interni per chunk) è il segnale operativo per accorgersene.
**Conseguenze**:
- `src/stage_3-1_prompt.py`: `PROMPT_VERSION = "0.3.0"`. SYSTEM_PROMPT esteso (interventi puntuali su sezione Event, sezione Event-vs-Reflection, sezione Densità; aggiornamento commento `_FEW_SHOT_FILES` e composizione della tupla). `EXTRACTION_TOOL.input_schema` invariato: Event interno non è un nuovo tipo, è un Event con regole di estrazione specifiche.
- `SCHEMA_VERSION` resta `0.1.0`: la shape Pydantic dei dati estratti non è cambiata. `EDGE_COMPATIBILITY` accetta già Event→Event per CAUSED/FOLLOWS/ECHOES/CONTRASTS_WITH e Event→Phase per DURING — l'aggancio obbligatorio dell'Event interno è coperto dallo schema esistente.
- `STAGE_VERSION` del runner `stage_3-2_extract.py` resta `0.1.0`: il comportamento del runner non è cambiato.
- I file in `data/stage_3/few_shots/*.json` vengono aggiornati: `ch_0127.json` rimosso, `ch_0092.json` aggiunto, `ch_0047.json` / `ch_0113.json` / `ch_0122.json` riannotati a 0.3.0 (estrazione di fatto invariata, ma marcatura di versione), `ch_0093.json` aggiunto come quarto few-shot con un Event interno e aggancio FOLLOWS al rientro ad Antiochia. I file in `data/stage_3/test/*.json` (Pila B) vanno rivisti a mano per allineamento alle nuove regole, in particolare per riconoscere gli Event interni non emersi sotto la regola 0.2.0. **Da fare prima del prossimo run** di estrazione, perché few-shot e gold di test incoerenti col prompt confondono il modello e inquinano la valutazione.
- Pila B di test ampliata con quattro chunk pensati come stress-test della regola 0.3.0: `ch_0026` (ritratto di Marullino + profezia notturna: Event interni candidati su atti puntuali dell'avo; eredità ancestrale; molteplici Person nodo isolate), `ch_0218` (lutto per Antinoo: catena di morti enumerate come antefatti, test della tenuta della regola antefatti-in-subordinata sotto pressione tematica; Event interno candidato sulla presa di coscienza), `ch_0011` (meditazione sull'erotica come conoscenza: prova di non-regressione su meditativo puro, zero Event interni attesi), `ch_0222` (Colosso di Memnone: due Event interni candidati — il capriccio di incidere il nome e il ricordo improvviso del compleanno di Antinoo — con catena CAUSED/FOLLOWS pulita).
- Debito tecnico individuato durante l'annotazione di `ch_0222`: l'arco `CONTRASTS_WITH` Event ↔ Theme (per il ricordo del compleanno di Antinoo che rovescia il tema dell'opposizione al tempo) non è ammesso da `EDGE_COMPATIBILITY`, che limita `CONTRASTS_WITH` a Event-Event o Theme-Theme. Da decidere: (a) estendere la compatibilità per ammettere Event-Theme; (b) rinunciare all'arco e ricostruire il contrasto al livello dei soli Theme. Decisione rinviata, non bloccante per 0.3.0.
- Soglia di test per validare la regola dei tre criteri di Event interno: su un campione di chunk estratti a 0.3.0, gli Event interni dovrebbero essere ≤15-20% degli Event totali. Sopra questa soglia, la regola non sta tenendo e (2) sta entrando di nascosto come Event — segnale che il pattern A va rivisto verso un tipo dedicato (Opzione C, bump di SCHEMA_VERSION).

### ADR-016 — Schema 0.2.0: Subject, Era, ruoli su INVOLVES
**Data**: 2026-05-22
**Stato**: attivo
**Contesto**: il grafo prodotto a 0.1.0 / 0.3.0 mostra effetto stella attorno ad Adriano (degree 854, ordini di grandezza oltre ogni altro nodo), Phase frammentate in cluster di sinonimi (5 nodi distinti per "malattia terminale"), 82% di Event privi di DURING. Diagnosi: la struttura ontologica non distingue il soggetto della biografia dagli altri nodi, INVOLVES non ha grana di ruolo, e l'asse temporale non ha una spina dorsale a set chiuso.
**Decisione**: bump SCHEMA_VERSION 0.1.0 → 0.2.0 con quattro modifiche.
1. **Subject come sottoclasse di Node** (label aggiuntivo `:Subject` in Neo4j). Sottoclasse Pydantic `Subject(Node)` con campi biografici opzionali (nome, cognome, soprannomi, anno e luogo di nascita, anno di morte, note). I campi NON sono estratti dal modello: vengono iniettati a configurazione prima del run e passati al prompt come blocco `<subject_profile>`. Il soggetto resta nodo nel grafo ma è nascosto di default nelle view (decisione di rendering, non di ontologia). Reversibile.
2. **Era come nuovo NodeType** (set chiuso: `infanzia`, `gioventu`, `adultita`, `vecchiaia`). Spina dorsale temporale universale, indipendente dal testo. Le 3 catene `TRANSFORMS_INTO` fra Era consecutive sono generate deterministicamente in stadio 3.5, non chieste al modello.
3. **Ruoli su INVOLVES**: nuovo campo `role` su `Edge` quando `type == INVOLVES`, valori `protagonist`, `participant`, `mentioned`. Permette di filtrare Adriano dalle view sopprimendo i `role=protagonist` su Subject senza perdere informazione.
4. **Nuovo edge type `OCCURS_IN`**: Phase → Era, aggancio delle Phase emergenti dal testo alla griglia chiusa delle Era. `EDGE_COMPATIBILITY` esteso. `TRANSFORMS_INTO` e `FOLLOWS` ammessi anche fra Era → Era (per le catene deterministiche).
**Razionale**: la combinazione di Subject (per disambiguare il soggetto), Era (per ancorare il tempo) e ruoli su INVOLVES (per pesare gli archi) risolve in un colpo tre patologie del grafo 0.1.0 senza distruggere la compatibilità — la migrazione da 0.1.0 è additiva. Portabile al prodotto: il cliente ha un Subject configurato all'onboarding, Era è universale, i ruoli su INVOLVES catturano la differenza tra "io c'ero" e "qualcuno me lo ha raccontato".
**Conseguenze**: `src/schema.py` modificato. `SCHEMA_VERSION = "0.2.0"`. Ri-estrazione necessaria per i chunk del prossimo run. Stadio 3.5 da aggiornare per generare le catene Era → Era. Le Phase emergenti restano (non sostituite da Era) ma con responsabilità ridotta: dettaglio temporale specifico, non spina dorsale.

---

### ADR-017 — Prompt 0.4.0: profilo del soggetto, ancoraggio Era, disambiguazione tipi
**Data**: 2026-05-22
**Stato**: attivo
**Contesto**: report diagnostico sul run 0.3.0 (`analisi_diagnostica_v0_3_0.md`). Pattern critici emersi: rapporto CAUSED/FOLLOWS = 0.31 (il modello legge cronaca, non narrazione), 497 Theme con coda lunga di sinonimi (`bellezza_e_indifferenza` vs `bellezza_e_sublime` ecc.), 17 warning di tipo discordante per stesso id (`morte_di_antinoo` Event vs Theme, `senato` Person vs Place, `aelia_capitolina` Work vs Place).
**Decisione**: bump PROMPT_VERSION 0.3.0 → 0.4.0 con sei modifiche al SYSTEM_PROMPT (le quattro pianificate sopra + tre raccomandazioni del report, di cui una collassa con il punto 2).
1. **Blocco `<subject_profile>` iniettato dinamicamente**: il prompt riceve il profilo del soggetto della biografia (nome canonico, anni, ruolo storico, persone significative note a priori). Sostituisce il vecchio paragrafo statico "Memorie di Adriano… narratore Adriano vecchio". La parte stilistica (narratore = protagonista, prima persona, alternanza scena/riflessione) resta nel SYSTEM_PROMPT come istruzione di interpretazione.
2. **Era e ancoraggio temporale obbligatorio**: nuova sezione che presenta Era (set chiuso) come tipo di nodo. Regola: ogni Event va ancorato a una Era via `DURING` (la stessa relazione semantica usata oggi per Phase), salvo Event puramente meditativi/atemporali. Le Phase emergenti restano ma sono dettaglio, non ancora primaria.
3. **Ruoli su INVOLVES**: nuova regola che chiede al modello di marcare ogni INVOLVES con `role` ∈ {protagonist, participant, mentioned}. Il `subject` (Adriano in questo run) è automaticamente `protagonist` di ogni Event in cui agisce; gli altri attori della scena sono `participant`; chi è solo citato senza agire è `mentioned`.
4. **Priorità CAUSED sopra FOLLOWS**: nuova regola nella sezione archi narrativi. Preferisci CAUSED quando il testo suggerisce un nesso (connettori "dopo che/perché/ormai/grazie a/per questo", conseguenza politica o emotiva, evidente implicazione narrativa). Riserva FOLLOWS a successioni puramente cronologiche (date, consolati, tappe enumerate). Aggiungere esempio nei few-shot.
5. **Canonicità del name del Theme**: il `name` di un Theme è sintagma breve generale (≤3 parole sostantive), pensato per riuso cross-chunk. La specificità del chunk va in `description`. Mira a comprimere la cardinalità dei Theme da ~500 a ~150-200.
6. **Disambiguazione del typing in 3 punti**:
   - **Eventi storici nominali** (Morte di Antinoo, Matrimonio con Sabina, Adozione di Lucio, battaglie): sempre Event, anche quando il chunk li richiama come tema. La dimensione tematica diventa un Theme separato collegato via EMBODIES.
   - **Istituzioni come attori collettivi** (Senato, Curia, Pretorianato che agiscono): Person. La sede fisica è un Place separato collegato via LOCATED_AT.
   - **Fondazioni edilizie** (Antinopoli, Aelia Capitolina, Villa Adriana): l'atto di fondare è Event, l'oggetto come luogo è Place, mai Work. Work resta riservato a opere mobili (libri, statue, leggi codificate, monete).
**Razionale**: tutte le modifiche sono additive al prompt esistente, basso costo in token e ad alto rendimento atteso secondo l'analisi diagnostica. Le tre regole del punto 6 risolvono ~15 dei 17 warning e prevengono gli analoghi silenziosi. La regola del punto 4 inverte il rapporto CAUSED/FOLLOWS verso 0.8-1.2, trasformando il grafo da cronaca a narrazione causale (obiettivo dichiarato dello stadio 3). La regola del punto 5 comprime la cardinalità Theme rendendo fattibile la deduplicazione di stadio 4.
**Conseguenze**: SYSTEM_PROMPT esteso di ~30-40 righe rispetto a 0.3.0. Few-shot esistenti vanno **rivisti**: aggiungere `role` ai INVOLVES, aggiungere `during` verso Era (oltre alle Phase esistenti), bilanciare CAUSED/FOLLOWS dove serve, accorciare i `name` dei Theme dove sono troppo specifici, aggiungere un esempio con `confidence: 0.55` su un'estrazione genuinamente dubbia (per dare al modello un'ancora di range basso, raccomandazione 8b del report). Cache caching invariato (ADR-013), si paga un cache write extra alla prima chiamata del run. Run di prova: primi 100 chunk per validare la nuova versione prima del run completo. Le decisioni di scope (cosa resta a stadio 4 / 6 / 7) sono esplicitamente registrate nel report e *non* affrontate nel prompt: deduplicazione Phase/Person/Theme → stadio 4; ECHOES e TRANSFORMS_INTO cross-chunk → stadio 6; confidence appiattita → non bloccante.

---

### ADR-018 — Prompt 0.4.2: calibrazione post-test Pila B
**Data**: 2026-05-27
**Stato**: attivo
**Contesto**: confronto qualitativo modello vs gold su 5 chunk di Pila B (`compare_test_results.json`, prompt 0.4.1). Tre pattern ricorrenti: Person/Place citati ma non estratti se privi di archi; `CONTRASTS_WITH` Theme↔Theme su tensioni desunte (4/5 chunk); grana Reflection instabile (accorpamento su paragrafi meditativi articolati, spacchettamento su apostrofi liriche unitarie).
**Decisione**: bump `PROMPT_VERSION` 0.4.1 → 0.4.2. Tre interventi puntuali al `SYSTEM_PROMPT` in `stage_3-1_prompt.py`:
1. **Nodi isolati obbligatori**: Person/Place nominati vanno estratti anche senza archi; test operativo "se togli il nome perdi informazione verificabile → estrai".
2. **`CONTRASTS_WITH` più restrittivo**: anti-pattern espliciti (gradi dello stesso continuum, temi complementari, tensione colpa/limite, simmetria poetica vita/morte); soglia = connettore avversativo o formula antitetica citabile in `evidence_span`.
3. **Reflection bidirezionale**: spacchettare quando cambiano soggetto/tempo/funzione; accorpare apostrofe lirica a voce omogenea (es. animula) in un'unica Reflection-invocazione.
**Razionale**: fix mirati alle patologie osservate nel test, senza bump di `SCHEMA_VERSION` né refactor dei few-shot. Costo token marginale; cache ADR-013 invariata.
**Conseguenze**: ri-estrazione consigliata su Pila B e su batch di validazione prima del full-run 310 chunk. `SCHEMA_VERSION` resta 0.2.0.

---

### ADR-019 — Full-run Prompt 0.4.2 - Schema 0.2.0
**Data**: 2026-05-27
**Stato**: completato
**Contesto**: dopo avanzamento a `Prompt_VERSION` 0.4.2 e ultimo test su batch. eseguito full run su tutti i 310 chunks 
**Conseguenze**: risultato buono, eseguito `extraction_analisys` per produrre `metrics.json` Nodi totali: 2467. Archi (triple uniche source/target/type): 4462.
alcune ridondanze di nomi di Event e Person evidenziano già dei punti su cui lavorare con stage_4.
Stage_3 **COMPLETATO**

---

### ADR-020 — Stadio 4: design della resolution
**Data**: 2026-05-28
**Stato**: attivo
**Contesto**: lo stadio 3 produce nodi chunk-locali. Lo stadio 4 deduplica e consolida in un `resolved_graph.json` con un nodo canonico per entità e provenance accorpata.

**Decisione**: lo stadio 4 è puramente deduplicazione strutturale, niente arricchimento semantico. Tre fasi indipendenti che producono mappe di decisioni, più un resolver che le applica.

1. **Fase 0 — Diagnostica.** Da `extracted_graph.json` deriva artefatti leggeri (`nodes_index.json`, `type_collisions.json`, `name_duplicates.json`). Sola lettura, deterministica.

2. **Fase A — Merge per name identico.** Per ogni gruppo di id con stesso `(type, name)` normalizzato, vince l'id con più occorrenze nel grafo (frequenza = canonicità d'uso). Niente euristiche linguistiche sul `name`. Pareggi, conflitti multi-direzione (id in più gruppi exact), e type-discordanti vanno in `to_review` con `canonical_id` da decidere a mano. Output: `merge_map.json` con sezioni `auto` e `to_review`. I casi gestibili dallo split sono esclusi dal review.

3. **Fase B — Split deterministico delle collisioni di tipo.** Per ogni id con type discordante, ogni occorrenza viene rietichettata `<id>__<type_lower>`. Niente whitelist, niente LLM, niente regex linguistiche. Le ~11 collisioni del run Adriano sono noise residuo, non un fenomeno semantico: trattarle come tale è overkill. Output: `split_map.json`.

4. **Resolver.** Preflight check (no `canonical_id` null, no `losers` inesistenti). Costruisce una rewrite map `(id_originale, type) → id_canonico` applicando **split prima, merge dopo**. Accorpa i nodi per `(id_canonico, type)` raccogliendo tutte le `provenances`; `name` e `description` scelte per frequenza, mai riscritte. Accorpa gli archi per `(source, target, type, role)` raccogliendo le `provenances`; archi INVOLVES con `role` diverso restano distinti (policy conservativa). Per gli split Event/Theme e Phase/Theme genera `Stage6Proposal` di tipo EMBODIES, tracciate in stage_4_log.json separato (insieme a merge applicati, split applicati, warning, statistiche).. Validazione `ResolvedGraph` Pydantic con integrità referenziale.
**Cosa NON fa lo stadio 4**:
- non fonde Theme near-duplicati (`bellezza` vs `bellezza_e_virtu`): non è duplicazione ma struttura tematica fine. Passa allo stadio 6.
- non genera archi EMBODIES: solo proposte tracciate.
- non riscrive `description` o `name`.

**Modelli**: i nodi e archi post-resolution vivono in `src/deduplication_schema.py` (`ResolvedNode`, `ResolvedEdge`, `ResolvedGraph`, `Stage6Proposal`), `DEDUP_SCHEMA_VERSION = "0.1.0"`. Separato da `schema.py` perché i campi `merged_from`/`merge_method`/`provenances: list[Provenance]` esistono solo dopo la resolution.

**Risultato sul run Adriano (PROMPT_VERSION 0.4.2 / SCHEMA_VERSION 0.2.0)**:
- input: 310 chunk, 4164 occorrenze nodo, 4542 occorrenze arco
- rewrite: 11 split, 21 merge
- output: 2460 nodi canonici, 4458 archi, 4 proposte stadio 6
- provenances per nodo: mediana 1, p95 3, max 298 (`adriano`)
- top hub: `adriano` (Person, 298), `adultita` (Era, 195), `roma__place` (Place, 74), `antinoo` (Person, 68), `vecchiaia` (Era, 57)

**Conseguenze**:
- stadio 3.5 (caricamento Neo4j) consuma `resolved_graph.json`, non `extracted_graph.json`.
- stadio 5 enrich raccoglie le `stage6_proposals` come input (rename previsto a `stage5_proposals`).
- i riferimenti "clinici" nei vecchi ADR (003, 004, 008) e in `schema.py` sono superati: il prodotto è una biografia navigabile del committente, non un'applicazione clinica. Da ripulire come manutenzione.

### ADR-021 — Archi risolti con tipi estremi; structure opzionale (stadio 4.4)
**Data**: 2026-06-03 (aggiornato 2026-06-08)
**Stato**: attivo

**Contesto**: per visualizzazione e filtri su archi flat serve conoscere il tipo di nodo sorgente e destinazione senza join su `nodes.json`. Il tool ad hoc `elements_splitter.py` non è in pipeline.

**Decisione**:
1. `ResolvedEdge` include `source_type` e `target_type` (`NodeType`), valorizzati in `stage_4-3_resolve.py` dalla tabella nodi canonici (`align_edge_endpoint_types` prima della scrittura).
2. `ResolvedGraph` valida allineamento tipi ↔ nodi e `EDGE_COMPATIBILITY` al momento del resolve.
3. `data/stage_4/3_resolve/resolved_graph.json` è la **fonte di verità** completa (nodi + archi con provenances). Stadi a valle (`4-5` health_checkup, `5` enrich, Neo4j) consumano solo questo file (+ `resolver_log.json` dove serve).
4. `stage_4-4_structure.py` è **opzionale**: proietta il grafo risolto in `data/stage_4/4_structure/{nodes,edges,provenance}.json` — file più snelli per caricamento ed esplorazione manuale (`nodes.json` e `edges.json` senza provenances; `provenance.json` con `nodes_provenances` e `edges_provenances` indicizzati per id/chiave arco, metadati completi).

**Cosa NON fa**: non tocca `schema.py` / `Edge` dello stadio 3; non richiede ri-estrazione. Bump solo `DEDUP_SCHEMA_VERSION` 0.1.0 → 0.1.1.

**Conseguenze**: re-run `stage_4-3_resolve` sullo stesso `extracted_graph.json`; `4-4` structure solo se servono le viste piatte. `elements_splitter.py` resta deprecabile a favore di `stage_4-4_structure.py`.

### ADR-022 — Health checkup al posto dello stadio 5 Validate; enrich e index rinumerati
**Data**: 2026-06-03
**Stato**: attivo

**Contesto**: lo stadio 5 Validate (report + flag manuali) era mal posizionato: la review umana ha più senso sul grafo **arricchito**; il QA automatico è utile **a fine di ogni stadio**; `merge_map.to_review` è già risolto nello stadio 4 (scelta `canonical_id` pre-resolve), non è backlog di validazione.

**Decisione**:
1. **Eliminare** lo stadio numerato 5 Validate. Spezzare le responsabilità:
   - **health_checkup** deterministico a fine stadio (`metrics` + `anomalies` + `health_log`);
   - **review umana** opzionale su `anomalies.json`, in workflow parallelo (flag `human_validated`, chiusura `review_needed`).
2. **Rinumerare**: enrich 6→**5**, index 7→**6**.
3. **Prima implementazione**: `stage_4-5_health_checkup.py` su `resolved_graph.json`; output in `data/stage_4/5_health_checkup/`.
4. Stessi contratti previsti per `stage_5-x` (post-enrich) e `stage_6-x` (post-index).

**Razionale**: checkpoint ripetibile e a basso costo; review umana sul deliverable completo post-enrich; allineamento al pattern sotto-stadi già usato in stadio 4.

**Conseguenze**: tabella globale PIPELINE aggiornata; `stage6_proposals` nel resolver log restano nome legacy fino al refactor enrich. `extraction_analysis.py` su grafo grezzo resta tool di sviluppo stadio 3, non sostituito da 4-5.

<!-- Aggiungere nuove ADR sopra questa riga, in ordine crescente di numero -->
### ADR-023 — Ristrutturazione dello stadio 5 (enrich): gerarchia tematica, macro-temi, policy di consolidamento
**Data**: 2026-06-15
**Stato**: attivo
**Contesto**: lo stadio 5 era abbozzato nella PIPELINE come elenco di compiti (ECHOES, EMBODIES, consolidamento Theme, TRANSFORMS_INTO) "da dettagliare in ADR dedicato". Durante l'implementazione sono emerse tre cose: (1) il consolidamento Theme non è deduplica lessicale ma giudizio semantico (i 307 Theme sono compositi, spesso senza token in comune — vedi `limite_e_vecchiaia` ~ `perdita_delle_capacita_fisiche`), che richiede embeddings + LLM locale; (2) il giudice produce in modo robusto, oltre ai `same`, una nutrita serie di **`refinement`** (tema specifico vs tema generale) che lo schema attuale non sa rappresentare e che stavamo per archiviare in un cassetto; (3) quei `refinement` sono in realtà lo scheletro di una **gerarchia tematica** ("la morte" come cappello di `morte_di_antinoo`, `vecchiaia_e_morte`, ecc.), che è il vero valore conversazionale del grafo finale ("segui *la morte* lungo tutta la vita"). Lo stadio 5 viene quindi ridefinito per "tirare le fila", non solo pulire.

**Decisione**:

1. **Macro-tema come FLAG, non nuovo NodeType.** Un tema-cappello è un nodo `Theme` con `is_macro=True` (in Neo4j etichetta aggiuntiva `:MacroTheme` oltre a `:Theme`). Stessa scelta architetturale di Subject:Person (ADR-016): reversibile, non duplica le righe di `EDGE_COMPATIBILITY` che toccano i Theme, lascia pulito lo schema dell'estrattore. Un cappello può essere un Theme **esistente promosso** (alza `is_macro`, tiene le sue provenienze reali) o un **nodo nuovo sintetizzato** dallo stadio 5 (provenienza sintetica). "Nodo nuovo" ≠ "tipo nuovo": vogliamo il primo.

2. **Nuovo `EdgeType` `SPECIALIZES`** (Theme → Theme, direzione **specifico → generale**). Materializza i `refinement` del giudice: `sete_di_potere_e_gloria → sete_di_potere`. `EDGE_COMPATIBILITY[SPECIALIZES] = ({THEME}, {THEME})`.

3. **Modifiche di schema (enrichment-only, NIENTE ri-estrazione).** L'estrattore (stadio 3) non emette mai `SPECIALIZES` né `is_macro`: l'output di stadio 3 è byte-identico. Stesso pattern delle catene `TRANSFORMS_INTO` Era→Era (vivono in schema ma sono generate a valle).
   - `src/schema.py`: aggiungere `SPECIALIZES = "SPECIALIZES"` a `EdgeType`; aggiungere `EdgeType.SPECIALIZES: ({NodeType.THEME}, {NodeType.THEME})` a `EDGE_COMPATIBILITY`. **`SCHEMA_VERSION` 0.2.0 → 0.3.0.**
   - `src/deduplication_schema.py`: aggiungere `is_macro: bool = False` a `ResolvedNode`. **`DEDUP_SCHEMA_VERSION` 0.1.1 → 0.2.0.**

4. **Provenienza degli artefatti sintetici dell'enrich** (convenzione unica per tutto lo stadio 5, consolida quanto già fatto al 5-1). Archi e nodi creati dall'enrich portano una `Provenance` con: `chunk_id` = un chunk REALE che àncora l'inferenza (per gli archi da split, un chunk dei nodi estremi; per i cappelli nuovi, un chunk dei sotto-temi); `model` = sentinella di sotto-stadio (`stage_5-1_embodies`, `stage_5-3_hierarchy`, ...) — NON un modello LLM quando la derivazione è deterministica; `confidence` ed `evidence_span` dalla decisione che li genera; `human_validated=False`.

5. **Sotto-stadi dello stadio 5 (ristrutturati):**
   - **5-1** EMBODIES da `Stage6Proposal` — *fatto*. (Nota: le proposte Phase/Theme sono saltate, `EMBODIES` non ammette Phase come sorgente; vedi punto aperto sotto.)
   - **5-2** Consolidamento Theme (merge dei `same`): `5-2a` candidati (BGE-M3 denso + Jaccard lessicale, *fatto*), `5-2b` giudizio LLM a 3 vie con evidence_span via LM Studio/Qwen3-14B (*fatto*, PROMPT_VERSION 0.3.0), `5-2c` resolver dei `same`.
   - **5-3** Gerarchia tematica (*nuovo*): dai `refinement` del 5-2b + giro mirato, posa gli archi `SPECIALIZES`, sintetizza/promuove i cappelli `is_macro`. Gestisce catene multi-livello e cicli.
   - **5-4** ECHOES Event→Event (embeddings + LLM).
   - **5-5** TRANSFORMS_INTO Phase/Person.
   - **5-x** health_checkup (pattern ADR-022).

6. **Policy di consolidamento del 5-2c** (i `same` del 5-2b):
   - **Cluster per componenti connesse** sui `same` (non per coppia): `anima_e_corpo==corpo_e_anima` e `anima_e_corpo==corpo_e_spirito` formano un'unica componente.
   - **Conflitto same×refinement interno**: se esiste un `refinement` con ENTRAMBI gli estremi nella stessa componente (il giudice ha chiamato la stessa relazione sia "same" sia "refinement"), l'intera componente NON si fonde e va in `conflict_clusters` per revisione. (Intercetta `corpo_e_spirito`, che è insieme `same` di `anima_e_corpo` e refinement di `corpo_e_anima`.) Un refinement con UN solo estremo nel cluster NON è conflitto (es. `lutto_e_dolore > lutto_per_antinoo`: `lutto_e_dolore` è fuori dal cluster `{lutto_per_antinoo, __theme}`): è legittimo, e dopo il merge il refinement si rimappa sull'id canonico.
   - **Soglia di confidence**: `cluster_confidence` = minimo delle confidence dei `same` interni alla componente (anello più debole). Componente senza conflitto e con `cluster_confidence ≥ 0.97` → **merge silenzioso** (applicato, `review_needed=False`). `< 0.97` → **NON applicato**, va in `review_clusters` (merge proposto, da confermare a mano abbassando `--min-confidence` o approvando la mappa).
   - **Merge mechanics** (come stadio 4): id/name/description canonici per **frequenza** (n. provenienze; tie-break alfabetico), mai riscritti; **tutte** le provenienze accorpate (le sfumature fini sopravvivono negli `evidence_span`); nomi alternativi dei membri salvati negli `aliases`; `merge_method="synonym_llm"`; archi che puntano ai temi fusi (gli EMBODIES del 5-1 inclusi) rimappati sull'id canonico, dedup per `(source, type, target, role)`, self-loop rimossi.
   - I **`refinement`** vengono rimappati sugli id canonici (per gli estremi appartenenti a cluster applicati) e passati al 5-3. NON diventano archi nel 5-2c.

**Razionale**: la combinazione macro-flag + `SPECIALIZES` trasforma il grafo da elenco piatto di temi a rete tematica navigabile, senza rompere la compatibilità (modifiche additive, enrichment-only). La policy a componenti connesse con intercettazione dei conflitti same×refinement è l'unico modo di fondere senza creare contraddizioni topologiche, ed è la naturale estensione del resolver di stadio 4 (ADR-020) al caso semantico. La soglia 0.97 per il merge silenzioso riflette che la confidence di Qwen3-14B non è pienamente affidabile: sopra è quasi sempre giusto, sotto conviene l'occhio umano (costo zero, poche decine di coppie).

**Conseguenze**:
- `schema.py` e `deduplication_schema.py` bumpati come al punto 3, **prima** di eseguire 5-2c. Nessuna ri-estrazione; il grafo grezzo di stadio 3 resta valido.
- Catena di artefatti dell'enrich (spina dorsale = `ResolvedGraph`, provenienze inline complete): 5-1 → `data/stage_5/1_embodies/enriched_graph.json`; 5-2c → `data/stage_5/2_themes/enriched_graph.json`; 5-3 → `data/stage_5/3_hierarchy/enriched_graph.json`; ecc. I file flat di `4_structure/` restano export esplorativi rigenerabili, mai fonte di verità (e vanno corretti perché le provenienze dei nodi includano i metadati, manutenzione separata).
- Il 5-2c produce `theme_merge_map.json` (decisioni ispezionabili: cluster applicati, in review, in conflitto; refinement rimappati per il 5-3; conteggi di rewrite).
- Embeddings via `sentence-transformers`/BGE-M3 (Python puro, modello locale); giudizio via LM Studio (OpenAI-compatibile, structured output JSON schema), Qwen3-14B-Q4_K_M. L'API esterna (es. Opus) resta opzione aperta per i giudizi sottili, dato il volume bassissimo (decine di coppie).

**Punti aperti** (non bloccanti, da decidere durante 5-3 o dopo):
- `EMBODIES` con sorgente `Phase`: la proposta `lutto_per_antinoo__phase → __theme` è stata saltata al 5-1 perché `EMBODIES` ammette solo `(Event|Person) → Theme`. Se il pattern Phase→Theme ricorre, valutare l'estensione di `EDGE_COMPATIBILITY[EMBODIES]` ad ammettere `PHASE` (ulteriore bump di `SCHEMA_VERSION`).
- Direzione dei `refinement`: il 5-2b valida che `general_id` sia uno dei due estremi; il 5-3 dovrà gestire catene multi-livello (cluster identità) e rompere eventuali cicli.

### ADR-024 — Architettura ricorrente dello stadio 5 (catena di artefatti + pattern candidati/giudice/build)
**Data**: 2026-06-24
**Stato**: attivo
**Contesto**: lo stadio 5 ha cinque sotto-stadi eterogenei (deterministici e LLM-based). Senza una struttura comune ognuno rischiava un formato e un flusso proprio, rendendo il debug e l'idempotenza difficili.
**Decisione**: tutti i sotto-stadi seguono lo stesso schema.
1. **Spina dorsale unica**: ogni sotto-stadio legge un `enriched_graph.json` (shape `ResolvedGraph`, provenienze inline complete) e ne scrive uno nuovo nella propria cartella `data/stage_5/<n>_<nome>/`. Niente formati flat come fonte di verità.
2. **Mappa di decisioni** ispezionabile accanto al grafo (`embodies_map`, `theme_merge_map`, `hierarchy_map`, `echoes_map`, `transforms_map`): nessun sotto-stadio "perde" una scelta in silenzio.
3. **Pattern semantico a tre tempi**: dove serve inferenza, **candidati deterministici** (embeddings BGE-M3, recall) → **giudice LLM locale** (Qwen3-14B via LM Studio, structured output JSON, bias conservativo) → **resolver/build** che applica. Le derivazioni puramente deterministiche (5-1, 5-3d) NON chiamano LLM.
4. **Idempotenza**: cache dei giudizi LLM per chiave `(elementi, model, prompt_version)`; bump del `prompt_version` = ri-giudizio. La rivalidazione `ResolvedGraph` gira sempre alla scrittura.
**Razionale**: un solo formato e un solo flusso rendono i sotto-stadi diffabili, riavviabili e componibili a catena. Embedder per il recall + LLM piccolo locale per la precisione tiene i costi a zero e i dati in casa (privacy, riproducibilità), coerente con l'uso futuro su biografie cliente.
**Conseguenze**: dipendenze locali (`sentence-transformers`/BGE-M3, `openai` verso LM Studio). LM Studio va avviato col modello caricato prima dei sotto-stadi LLM. L'API esterna (Opus) resta opzione per i pochi giudizi sottili.

---

### ADR-025 — Gerarchia tematica costruita in quattro passi (5-3a/b/c/d)
**Data**: 2026-06-24
**Stato**: attivo
**Contesto**: ADR-023 prevedeva la gerarchia tematica (`SPECIALIZES` + macro-temi `is_macro`) ma non il *come*. I `refinement` del 5-2b coprono solo le coppie giudicate; restano ~290 temi orfani da organizzare, ed esiste il rischio di cicli e di cappelli mal definiti.
**Decisione**: quattro passi separati, dal deterministico al semantico al deterministico.
1. **5-3a candidati** (deterministico): assembla un seed DAG dai `refinements_for_5_3` (rottura cicli rimuovendo l'arco a confidence minima) e clusterizza TUTTI i Theme per coseno BGE-M3 (agglomerativo, soglia su distanza). Output: cluster + seed.
2. **5-3b giudizio cluster** (Qwen): per ogni cluster ≥2 propone fino a 2 cappelli (preferendo un membro esistente) con i loro membri e lascia gli outlier in `unattached`. I refinement del seed presenti nel cluster sono iniettati come "già deciso". Normalizzazione che garantisce una partizione (niente cappelli-fantasma, niente doppie assegnazioni).
3. **5-3c aggancio orfani** (Qwen): `unattached` + singoletti vengono agganciati a un cappello (shortlist BGE-M3 → decisione Qwen `specializes`/`standalone`, in dubbio standalone). Gli orfani non si agganciano mai fra loro.
4. **5-3d build** (deterministico): unisce le tre fonti di archi (seed > judgment > assignment), rompe i cicli del grafo combinato, posa i `SPECIALIZES` (Theme→Theme, specifico→generale) e marca i cappelli `is_macro`. I cappelli esistenti sono **promossi** (provenienze reali intatte); i nuovi sono **sintetizzati** con provenienza ancorata a un chunk di un sotto-tema e `review_needed=True`.
**Razionale**: separare recall (embeddings) da precisione (LLM) da applicazione (build deterministico) rende ogni passo ispezionabile e riavviabile. La sintesi del cappello come nodo nuovo (non nuovo tipo) realizza la scelta di ADR-023; la promozione preserva le provenienze.
**Conseguenze sul run Adriano**: 305 Theme, 97 cluster (34 singoletti), 95 orfani, 71 cappelli → **208 archi `SPECIALIZES`**, 41 cappelli promossi + 30 sintetizzati. Nessun ciclo residuo. Il node-merge NON avviene qui: il conflict cluster `anima/corpo` resta separato (entrambi ricevono `SPECIALIZES` verso "Il corpo").

---

### ADR-026 — ECHOES e TRANSFORMS_INTO come archi sciolti, bias conservativo (5-4, 5-5)
**Data**: 2026-06-24
**Stato**: attivo
**Contesto**: a differenza della gerarchia (struttura globale), `ECHOES` e `TRANSFORMS_INTO` sono archi locali "questa scena richiama quest'altra" / "questa fase evolve in quest'altra". Il rischio è sovra-generare connessioni da semplice affinità tematica (già catturata dai Theme) o da semplice successione cronologica (già implicita).
**Decisione**: un modulo per tipo, stesso pattern candidati→giudizio→posa, con bias verso il "no".
1. **5-4 ECHOES** Event→Event: candidati per coseno sulla `description`, **esclusi i chunk adiacenti** (`min_chunk_gap`, default 3 — l'eco collega scene lontane, non la scena accanto che è già FOLLOWS/CAUSED). Qwen distingue eco vero (esplicito o strutturale) da duplicato dello stesso fatto e da affinità vaga. Direzione: il source richiama il target (la fonte, più antica). Posa solo se `confidence ≥ 0.6`.
2. **5-5 TRANSFORMS_INTO** Phase→Phase: candidati = Phase consecutive nella stessa Era (ordine per chunk medio); Qwen distingue metamorfosi (continuità + cambio di stato) da semplice successione. `Person→Person` **fuori scope** (dopo dedup il soggetto è un nodo solo); `Era→Era` già fatto deterministicamente in 3.5.
**Razionale**: il bias conservativo protegge il valore del grafo (un eco/transform spurio è peggio di uno mancato); l'esclusione delle coppie adiacenti e il vincolo intra-Era evitano di duplicare relazioni già presenti. Gli archi sintetici portano `review_needed=True` e provenienza-sentinella per essere filtrabili.
**Conseguenze sul run Adriano**: 5-4 → 665 Event, 122 candidati, **8 `ECHOES`**. 5-5 → 66 Phase su 4 Era, 62 coppie consecutive, **14 `TRANSFORMS_INTO`**. Numeri volutamente bassi, coerenti col bias.

---

### ADR-027 — Stadio 5 completato: stato del grafo arricchito e punti aperti
**Data**: 2026-06-24
**Stato**: completato
**Contesto**: chiusura dello stadio 5 dopo l'esecuzione e la validazione dei cinque sotto-stadi sul run Adriano (PROMPT 0.4.2 / SCHEMA 0.3.0 / DEDUP 0.2.0).
**Conseguenze**: grafo arricchito finale (`data/stage_5/5_transforms/enriched_graph.json`) ~2488 nodi (71 macro-temi) e ~4691 archi, partendo dai 2460 nodi / 4461 archi dello stadio 4. Apporti dell'enrich: 3 `EMBODIES`, 208 `SPECIALIZES`, 8 `ECHOES`, 14 `TRANSFORMS_INTO`; consolidamento Theme (2460 → 2458 nodi); 71 cappelli `is_macro`. Schema e dedup_schema bumpati senza ri-estrazione (output stadio 3 invariato). Stage_5 **COMPLETATO**.
**Punti aperti** (non bloccanti, da affrontare prima/durante l'indice):
- `health_checkup` automatico post-enrich: **implementato** (`5-6`, ADR-028, verdetto `pass_with_warnings`). Resta la **review umana** sul grafo arricchito (coda di 65 item in `review_queue.json`).
- conflict cluster `anima_e_corpo`/`corpo_e_anima`/`corpo_e_spirito` lasciato non fuso dal 5-2c: decidere la promozione manuale.
- 4 cluster `theme` sotto soglia 0.97 in `review_clusters`: confermare o scartare i merge a mano (`--update`).
- proposta `EMBODIES` con sorgente `Phase` saltata: valutare l'estensione di `EDGE_COMPATIBILITY[EMBODIES]` a `PHASE` (ulteriore bump `SCHEMA_VERSION`) se il pattern ricorre.

### ADR-028 — Health checkup dello stadio 5 (5-6): review_needed atteso, non bloccante
**Data**: 2026-06-24
**Stato**: attivo
**Contesto**: lo stadio 5 chiudeva senza checkpoint automatico (punto aperto ADR-027). Il pattern `health_checkup` (ADR-022) era già usato in stadio 3 e 4, ma il contratto dello stadio 4 tratta `review_needed=True` come **bloccante** (decisione di resolution non chiusa). Nell'enrich è l'opposto: EMBODIES, ECHOES, TRANSFORMS_INTO e i cappelli sintetizzati portano `review_needed=True` **di proposito**, come coda di review umana pre-index. Applicare la regola di stadio 4 darebbe `fail` su un grafo sano.
**Decisione**: `src/stage_5-6_health_checkup.py` (deterministico, zero LLM), output in `data/stage_5/6_health_checkup/` con lo stesso contratto (`dashboard.html`, `checks.json`, `metrics.json`, `review_queue.json`, `health_log.json`). Differenze rispetto al 4-5:
1. **`review_needed` enrich = atteso (info), non blocca.** Gli elementi `review_needed` di origine enrich (riconosciuti per provenienza `model=stage_5*` / `merged_from=stage5_*`, non per tipo di arco) confluiscono nella `review_queue`. Solo un `review_needed` di origine NON-enrich è un residuo anomalo (warn).
2. **Contributo enrich misurato per provenienza, non per tipo**: EMBODIES ed ECHOES esistono già nell'estrazione, quindi il conteggio per tipo sovrastimerebbe l'apporto del 5. Sul run: 233 archi aggiunti (3 EMBODIES, 208 SPECIALIZES, 8 ECHOES, 14 TRANSFORMS_INTO).
3. **Check specifici dell'enrich**: gerarchia `SPECIALIZES` aciclica (DAG, **blocca** se fallisce), macro-temi senza figli (cappelli vuoti), decisioni Theme pendenti del 5-2c (review + conflict cluster), proposte EMBODIES saltate (Phase→Theme). Le mappe dei sotto-stadi sono lette se presenti (degrada con grazia se assenti).
**Razionale**: un checkpoint ripetibile e a basso costo che certifica il grafo finale senza confondere la coda di review (sana) con un errore. La distinzione per provenienza è l'unico modo per pesare correttamente il contributo dell'enrich e i flag di review.
**Conseguenze**: verdetto sul run Adriano `pass_with_warnings` (0 fail; warn su decisioni Theme pendenti e hub da spot-check), `review_queue` di 65 item. `STAGE_VERSION 0.1.0`. La review umana resta il passo successivo prima dell'indice.

### ADR-029 — Stadio 6 (index): separazione build/inferenza, artefatti in `data/stage_6/`, contratto via manifest
**Data**: 2026-06-24
**Stato**: attivo
**Contesto**: la cartella `inference/` era nata mentre lo stadio 5 era ancora in lavorazione, e teneva insieme due responsabilità: (1) **costruzione** dell'indice (embedding dei chunk, serializzazione) e (2) **inferenza** vera e propria (retrieval + LLM, agente conversazionale, server web). Questo confondeva i confini: l'indexing è un passo deterministico della pipeline (al pari di chunking o resolve), mentre l'inferenza è il consumo a valle. `build_index.py` viveva in `inference/`, l'indice in `inference/data/index/`, fuori dalle convenzioni della pipeline (niente `STAGE_VERSION`, log, manifest, health_checkup). Inoltre `inference/config.yaml` puntava ancora a `resolved_graph.json` (stadio 4) in alcune parti della doc, mentre il grafo corretto è `enriched_graph.json` (stadio 5).
**Decisione**: formalizzare l'indicizzazione come **Stadio 6** dentro `Adriano_graph/`, e ridurre `inference/` alla sola inferenza.
1. **Build → pipeline.** `src/stage_6-1_index.py` (deterministico, zero LLM) produce gli artefatti dell'indice in `data/stage_6/1_index/`: `vectors.npy`, `meta.json`, `chunk_texts.json`, `manifest.json`, `index_log.json`. Stesso embedder BGE-M3 dello stadio 5, `normalize_embeddings=True` (coseno = prodotto scalare).
2. **Contratto via manifest, niente duplicazione del grafo.** Il `manifest.json` registra la provenienza completa delle sorgenti (chunk e grafo: path + `sha256` + versioni + conteggi) e i parametri di embedding. Il grafo NON viene copiato in stage_6: l'inferenza lo legge da `stage_5/5_transforms/enriched_graph.json` e valida la coerenza confrontando l'hash col manifest (avviso non bloccante se difforme). Scelta "manifest" vs "bundle" del grafo: meno duplicazione, fonte di verità unica del grafo allo stadio 5.
3. **Health checkup (6-2).** `src/stage_6-2_health_checkup.py` (pattern ADR-022/028) certifica l'indice: artefatti presenti/coerenti (blocca), allineamento indice↔chunk e indice↔grafo, **copertura nodi** (% di nodi raggiungibili via chunk indicizzati; Era e nodi sintetici scoperti sono attesi), **smoke retrieval** (5 domande, ≥1 chunk e ≥1 nodo, senza LLM). Output in `data/stage_6/2_health_checkup/`.
4. **Inferenza ridotta al consumo.** Rimosso `inference/build_index.py`; `inference/rag/index.py` espone solo `load`+`search` (più `load_manifest`); `config.yaml` punta `index_dir` a `../Adriano_graph/data/stage_6/1_index` e `graph_path` a `enriched_graph.json`; `session.py` legge il manifest all'avvio e avvisa se il grafo è cambiato dopo la build. `smoke_test.py` resta come smoke *dell'agente* (end-to-end con LLM), distinto dal checkup deterministico del 6-2.
**Razionale**: separare build e inferenza rende l'indice un artefatto versionato, ispezionabile e idempotente come ogni altro stadio, e libera `inference/` per evolvere come prodotto conversazionale senza trascinare la logica di indicizzazione. Il manifest è il contratto minimo che garantisce la consistenza indice↔sorgenti senza copiare file pesanti, coerente col principio "niente over-engineering".
**Conseguenze**:
- `inference/data/index/` deprecata a favore di `Adriano_graph/data/stage_6/1_index/`; rigenerare con `python src/stage_6-1_index.py` (richiede BGE-M3).
- Nuove voci in `DEPENDENCIES.md` per gli stadi 6 (sentence-transformers, numpy già presenti).
- **Punti aperti**: (a) review umana post-enrich (coda 5-6) prima dell'indice definitivo; (b) indicizzazione semantica dei nodi/temi (oggi indice sui soli chunk) come eventuale `stage_6-3`, per un ibrido pieno (macro-temi e `SPECIALIZES` oggi raggiunti solo via chunk).

<!-- Aggiungere nuove ADR sopra la riga finale di PIPELINE.md, in ordine crescente -->