# Pipeline — Prototipo Knowledge Graph Biografico

> **Progetto**: trasformare una narrazione biografica in un knowledge graph navigabile, da cui alimentare un agente conversazionale in prima persona. Obiettivo a lungo termine: life review in cure palliative. Banco di prova: *Memorie di Adriano* di Marguerite Yourcenar (trad. Storoni Mazzolani).
>
> **Documento vivo**: aggiornare ad ogni cambio di stato o decisione. Le decisioni di design vanno in coda come ADR datati, non vengono cancellate.

---

## Stato globale

| Stadio | Nome | Stato | Output principale |
|---|---|---|---|
| 0 | `stage_0_extract_pdf` | fatto (v0.2.0) | `raw_text.txt` + `structure.json` + `extraction_log.json` |
| 1 | `stage_1_clean` | fatto (v0.1.0) | `cleaned_text.txt` + `cleaning_log.json` + `inspection_report.json` |
| 2 | `stage_2_chunk` | fatto (v0.1.0) | `chunks.json` + `chunking_log.json` |
| 3 | `stage_3_extract` (sub-stages `3-1_prompt`, `3-2_extract`) | in corso — prompt, few-shot e runner pronti, da validare sui 4 chunk di test | `extracted_graph.json` (JSON intermedio, NO Neo4j diretto) |
| 3.5 | `stage_3_5_load_to_neo4j` | da definire | grafo su Neo4j |
| 4 | `stage_4_resolve` | da fare | grafo deduplicato |
| 5 | `stage_5_validate` | da fare | report validazione + flag manuali |
| 6 | `stage_6_enrich` | da fare | grafo arricchito (tematico, riflessivo) |
| 7 | `stage_7_index` | da fare | indice RAG ibrido |

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

**Espansioni future** (non ora):
- Supporto a PDF con note a piè di pagina.
- Supporto a PDF con OCR (image-based) tramite `tesseract`.
- Estrazione di metadati strutturali (TOC, autore, ecc.) per progetti con più volumi.

---

## Stadio 1 — Cleaning

**Scopo**: applicare correzioni deterministiche al testo grezzo, con log ispezionabile di ogni modifica. Slot architetturale che resta nella pipeline anche quando vuoto (servirà per le trascrizioni cliniche).

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

**Per il caso clinico**: qui vivranno le correzioni domain-specific (nomi paziente ricorrenti, gergo medico mal trascritto). Lo slot è pronto.

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
**Output**: `data/stage_3/extracted_graph.json` — **JSON intermedio**, NON scrittura diretta su Neo4j.

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

## Stadi 4-7 — Da definire

Verranno specificati quando ci arriviamo. Schema generale già noto:
- **4 — Resolve**: deduplicazione entità (Antinoo menzionato in 30 chunk = 1 nodo).
- **5 — Validate**: report di qualità, flag per validazione umana, statistiche.
- **6 — Enrich**: passaggi LLM aggiuntivi su archi tematici/riflessivi che richiedono visione cross-chunk.
- **7 — Index**: costruzione indice RAG ibrido (vettoriale + grafo) per l'agente.

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
│   └── stage_3/
│       └── extracted_graph.json         ← prossimo
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
│   └── ...
├── notebooks/
│   └── 00_inspect_source.ipynb          (ricognizione PDF)
├── PIPELINE.md                          (questo file)
├── .env                                 (ANTHROPIC_API_KEY, NEO4J_*)
└── environment.yml
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
**Razionale**: Risolve alla radice sia il problema dei paragrafi sia quello degli errori OCR-like. Lo stadio 0 isola tutta la complessità formato-specifica in un punto solo, mantenendo gli stadi successivi puliti e riusabili (quando arriverà la fase clinica, basterà sostituire stadio 0 con `stage_0_transcribe`).
**Conseguenze**: Serve scrivere uno stadio 0 robusto e ispezionabile. Lo stadio 1 (cleaning) probabilmente avrà un dizionario quasi vuoto per Yourcenar, ma resta come slot architetturale.

---

### ADR-004 — Cleaning trasparente a regole, non LLM-assistito
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Per correggere eventuali errori residui di estrazione si poteva scegliere tra (1) regole regex deterministiche, (2) un passaggio LLM che propone correzioni, (3) niente cleaning esplicito.
**Decisione**: Opzione 1, con dizionario in `config/cleaning_rules.yaml` e log dettagliato di ogni sostituzione.
**Razionale**: Deterministico, ispezionabile, idempotente, allineato al requisito clinico di tracciabilità non-negoziabile. Un LLM rischia di parafrasare in silenzio, e per un testo letterario ogni alterazione semantica è grave. Lo stesso slot servirà per le trascrizioni cliniche dove il dizionario sarà domain-specific (nomi pazienti, termini medici ricorrenti mal trascritti).
**Conseguenze**: Compilazione del dizionario incrementale, basata su anomalie osservate. Per Yourcenar probabilmente quasi vuoto.

---

### ADR-005 — Paragrafi rilevati per indentazione, non per riga vuota
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: La prima versione dello stadio 0 (v0.1.x) identificava i confini di paragrafo solo tramite riga vuota nel PDF. Funziona su PDF con interlinea ampia tra paragrafi, fallisce sull'edizione Storoni Mazzolani che usa rientro tipografico classico. Risultato iniziale: 12 "paragrafi" giganti su 521k caratteri.
**Decisione**: Estendere lo stadio 0 (v0.2.0) a leggere le coordinate `x0` delle prime parole di riga via `pdfplumber.extract_words`, calcolare la mediana per pagina, marcare come inizio paragrafo le righe la cui prima parola è indentata oltre soglia (`mediana + 5pt`). Mantenere la riga vuota come secondo segnale (OR logico).
**Razionale**: La struttura del documento sorgente va preservata dove esiste, non ricostruita a valle con euristiche post-hoc su punteggiatura. Risultato: 322 paragrafi su 170 pagine processate, ~1.9 per pagina, coerente con il layout visivo del PDF.
**Conseguenze**: Stadio 0 a v0.2.0. Stadio 1 va rilanciato sul nuovo `raw_text.txt`. Formato dei file di output invariato (solo `paragraph_detection_method` aggiunto come metadato in `structure.json`). 
**Lezione metodologica**: trasferibile al caso clinico (analoghi: pause vocali, esitazioni, cambi di tono — informazioni che esistono nel segnale e che il primo stadio deve catturare, non far perdere e poi inseguire con regex).

---

### ADR-006 — Chunking 1-paragrafo-1-chunk con accorpamento dei soli paragrafi cortissimi
**Data**: 2026-05-13
**Stato**: attivo (supersede parzialmente ADR-002 sui parametri concreti)
**Contesto**: ADR-002 prevedeva chunking strutturale con vincoli min ~200 / max ~800 token, basato sull'ipotesi di paragrafi brevi (50-70 token) da accorpare. Dopo recupero corretto dei paragrafi (ADR-005), si osserva che la media reale è ~400-500 token per paragrafo. L'accorpamento perde significato per la maggioranza dei casi.
**Decisione**: Strategia "1 chunk = 1 paragrafo" come regola di base, con un'unica eccezione: paragrafi sotto soglia minima (80 token) vengono accorpati al paragrafo successivo (o al precedente se sono l'ultimo della parte). Nessuna soglia massima: paragrafi lunghi restano interi (rispetto dell'unità retorica autoriale). Confini duri sulle sei parti preservati.
**Razionale**: Massima granularità della provenienza (un nodo del grafo proviene da esattamente un paragrafo identificabile), massimo rispetto della struttura autoriale. La soglia minima di 80 token cattura i rari paragrafi monofrase senza introdurre complessità. Allineato al caso clinico: turni di parlato lunghi/brevi alterneranno, e l'accorpamento dei turni cortissimi è lo stesso problema.
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
- La provenienza arricchita (chunk_id, model, timestamp, confidence, evidence_span, human_validated) è disegnata su misura per il requisito clinico. Costruirla sopra `Node.metadata` di LlamaIndex è un combattimento contro l'astrazione.
- LlamaIndex evolve rapidamente. Dipendere dal framework per la parte centrale (estrazione) della pipeline che deve girare un domani in ambito clinico è un rischio.
- Quando arriveremo allo stadio 7 (RAG ibrido), LlamaIndex resta candidata per *quella parte*, importando il JSON intermedio. Da rivalutare allora.
**Conseguenze**: codice di estrazione tutto sotto controllo, dipendenze ridotte (solo `anthropic` e `pydantic`), prompt italiano nativo. Sostituibilità futura (modelli locali via Ollama/vLLM) diventa banale: cambia solo la funzione di chiamata.

---

### ADR-008 — Few-shot prompting con esempi versionati, NO zero-shot
**Data**: 2026-05-13
**Stato**: attivo
**Contesto**: Per l'estrazione si poteva scegliere tra zero-shot (solo istruzioni + schema), few-shot (istruzioni + esempi reali), o pre-elaborazione in due passi (LLM riassume → LLM estrae).
**Decisione**: Few-shot con 3-5 esempi annotati a mano su chunk veri del libro, conservati in `config/extraction_examples.yaml`. Versionati: quando cambiano, si bumpa la versione del prompt di estrazione e si ri-estrae.
**Razionale**:
- Gli esempi fungono da "contratto" canonico di cosa è Event e cosa è Reflection nel progetto. Sono la documentazione su cui costruire fiducia con stakeholder clinici (psicologi): più trasparente di "il modello è bravo".
- Migliorano la qualità su modelli grandi e diventano *indispensabili* su modelli locali piccoli (7-8B parametri) che potrebbero essere usati nel caso clinico per ragioni di privacy/costo. Il prompt funge da training in-context.
- Disaccoppiamento dominio/schema: lo schema resta invariato, gli esempi diventano "configurazione di dominio" (per Yourcenar e poi per il caso clinico due insiemi diversi).
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
- Il prototipo dovrà girare anche su altri testi (caso clinico). Per cambiare dominio servirà sostituire `stage_3_prompt.py` (e gli esempi in `data/stage_3/few_shots/`), non riscrivere lo schema. Tenerli separati rende la portabilità un cambio di modulo, non un refactor strutturale.
- `SCHEMA_VERSION` e `PROMPT_VERSION` ora hanno semantiche distinte e usabili: un'iterazione sulle istruzioni del prompt non invalida i dati estratti rispetto allo schema (basta bumpare `PROMPT_VERSION` per tracciare la differenza nei run).
- Il formato flat del tool minimizza i campi richiesti al modello: il modello produce solo informazione semantica (cosa estrae e perché), il sistema aggiunge automaticamente i metadati tecnici. Riduce token, semplifica l'output e isola le responsabilità.
- I file few-shot in formato fat sono leggibili come prodotti finiti dell'annotazione (la stessa shape che il grafo finale avrà), utili per validazione manuale e ispezione. Convertirli in flat in memoria non costa nulla.
**Conseguenze**: `EXTRACTION_INSTRUCTIONS` rimosso da `schema.py`. `stage_3_prompt.py` esistente popolato con `SYSTEM_PROMPT` riscritto (mantiene il taglio Yourcenar, sostituisce la chiusa "output JSON valido" con "invoca `submit_extraction`"), `FEW_SHOT_EXAMPLES` derivato da disco. `stage_3_extract.py` (prossimo) dovrà ricostruire `Provenance` a partire da `chunk_id` runtime + `model` + `timestamp` + `SCHEMA_VERSION` + i `confidence`/`evidence_span` flat ricevuti dal modello, prima di costruire `Node`/`Edge` Pydantic. La citazione "EXTRACTION_INSTRUCTIONS di schema.py" in ADR-007 resta come riferimento storico, superseduta sul punto da questo ADR.

---

### ADR-013 — Prompt caching a due breakpoint sul prefisso stabile
**Data**: 2026-05-14
**Stato**: attivo
**Contesto**: Misurazione con `tiktoken` (cl100k_base) sul payload di stadio 3 dopo l'introduzione di few-shot e tool: SYSTEM_PROMPT ~1.748 tok, tool schema ~461 tok, 4 esempi few-shot ~11.813 tok, totale prompt fisso ~14.000 tok ad ogni chiamata, contro un `chunk_text` corrente di ~500-1.000 tok. Cioè il 95% del costo per chunk è prompt fisso che NON cambia attraverso i 310 chunk. Valutate altre leve per ridurre token (troncare le `description` nei few-shot, rimuovere quelle degli archi, scendere a 3 esempi, comprimere il SYSTEM_PROMPT) ma tutte trade-offano qualità semantica contro token, e la qualità è prioritaria, soprattutto in vista dell'uso futuro su modelli più piccoli (Sonnet basic, 7-8B locali per il caso clinico) in cui esempi ricchi di `description` sono didatticamente più importanti, non meno.
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

<!-- Aggiungere nuove ADR sopra questa riga, in ordine crescente di numero -->
