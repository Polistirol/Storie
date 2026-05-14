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
| 3 | `stage_3_extract` | in corso — fase prompt design | `extracted_graph.json` (JSON intermedio, NO Neo4j diretto) |
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
│   ├── cleaning_rules.yaml              ✓ fatto (tutte disabled per Yourcenar)
│   └── extraction_examples.yaml         ← da scrivere a mano per stadio 3
├── src/
│   ├── schema.py                        ✓ fatto
│   ├── stage_0_extract_pdf.py           ✓ fatto (v0.2.0)
│   ├── stage_1_clean.py                 ✓ fatto (v0.1.0)
│   ├── stage_2_chunk.py                 ✓ fatto (v0.1.0)
│   ├── stage_3_extract.py               ← prossimo
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
<!-- Aggiungere nuove ADR sopra questa riga, in ordine crescente di numero -->
