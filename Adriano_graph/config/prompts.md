#production 
# SELEZIONE CHUNKS FEW-SHOTS - TEST 
## Prompt per agente Cursor — selezione chunk per few-shot e test set

### Contesto

Sto costruendo una pipeline che estrae un knowledge graph da una narrazione biografica (*Memorie di Adriano* di Yourcenar, traduzione italiana). Lo stadio 3 della pipeline usa Claude con tool use forzato per estrarre nodi e relazioni da chunk di testo. Il prompt di estrazione include esempi few-shot e viene validato contro un test set annotato a mano.

Per scegliere bene quali chunk usare come few-shot e quali come test set, mi serve prima capire la **distribuzione** dei 310 chunk: quali sono tipici, quali sono casi limite, quali sono pieni di segnale, quali sono poveri.

### Cosa devi fare

Leggi @Adriano_graph/data/stage_2/chunks.json e @Adriano_graph/src/schema.py per capire il formato dei chunk e cosa la pipeline cerca di estrarre (tipi di nodo: Event, Person, Place, Theme, ecc.; tipi di relazione).

Poi analizza tutti i 310 chunk e proponimi due liste:

1. **Candidati few-shot** (~6-10 chunk). Devono coprire la varietà di casi che l'estrattore incontrerà: scene narrative dense, riflessioni astratte, dialoghi, descrizioni di luoghi, eventi storici, stati interni, salti temporali. Devono essere chunk dove la struttura "giusta" da estrarre sia abbastanza chiara da poter essere annotata a mano senza ambiguità.

2. **Candidati test set** (~10-15 chunk). Devono includere sia casi tipici sia casi limite (chunk corti, chunk densissimi, chunk con poche entità nominate, chunk con molti riferimenti impliciti, chunk con stati emotivi contrastanti, chunk metaforici). Servono a stressare l'estrattore.

I due insiemi devono essere **disgiunti**.

L'estrattore lavora a grana di scena: una scena narrativa = un Event, e i dettagli interni (gesti, stati, sguardi) stanno dentro la sua descrizione, non come nodi separati. Eccezioni rare per contrasti espliciti. I tipi di nodo principali sono Event, Person, Place, Theme

### Vincoli

- **Non estrarre nulla.** Non devi produrre annotazioni di knowledge graph. Solo selezionare chunk.
- Non leggere @Adriano_graph/src/stage_3-1_prompt.py né altri prompt esistenti: devono restare un controllo cieco.
- Non modificare file di codice. Output solo come messaggio in chat o come singolo file `data/chunk_selection_proposal.md`.

### Output atteso

Per ogni chunk proposto:
- `chunk_id`
- Categoria assegnata (es. "scena narrativa densa", "riflessione astratta", "caso limite: chunk corto", ecc.)
- 1-2 righe di motivazione: perché questo chunk è rappresentativo di quella categoria
- Lunghezza in token (approssimativa va bene)

In testa, una breve sezione (10-15 righe) con la **distribuzione complessiva** che hai osservato nei 310 chunk: che tipi di contenuto ricorrono, qual è la lunghezza media/mediana, ci sono outlier, ecc. Serve a me per validare le tue scelte.

questo lavoro è già stato fatto con una versione di @Adriano_graph/src/schema.py  precednete, e ha prodotto @Adriano_graph/notebooks/chunk_selection_proposal.md .
puoi partire da li per vedere se ci sono chunks che ancora valgono come buoni

Niente preamboli, niente report lunghi. Lavora e produci le liste.

## ESTRAI 
estrai tu i 4 di few shot scelti, 
salva ognuno singolarmente dentro @Adriano_graph/data/stage_3/few_shots come ch_xxxx.json , 
la struttura attesa è in esempio dentro @Adriano_graph/data/stage_3/few_shots/old/ch_format_demo.json . 
dovresti usare il prompt di @Adriano_graph/src/stage_3-1_prompt.py e @Adriano_graph/src/schema.py come istruzioni di estrazioni.
poi fai i top 4 test, stessa cosa ma salvali in @Adriano_graph/data/stage_3/test 

# VALIDAZIONE chunks di test human vs model 
mi stai aiutando a validare l'estrazione di informazione su dei chunks di testo per creare un knowledge graph.
il testo di riferimento è memorie di adriano della yourcenar. e il kg risultante deve essere una versione esplorabile della biografia di adriano. questo testo è il banco di prova, verrà poi fatto con la storia raccontata dai clienti a me,
i file che ci interessano ora sono @Adriano_graph/src/schema.py  e @Adriano_graph/src/stage_3-1_prompt.py che contengono chema e regole di estrazione. @Adriano_graph/PIPELINE.md  contiene il flusso di lavoro.
il primo compito è verificare la bonta delle estrazioni di test fatte dal modello vs quelle fatte da me.
 @extracted_graph_test.json contine le estrazioni del modello, mentre dentro @Adriano_graph/data/stage_3/test ci sono le mie estrazioni . @Adriano_graph/data/output/compare_test_results.json  è un file che raccoglie per metriche numeriche le due estrazioni.
le estrazione del modello sono eseguite con @Adriano_graph/src/stage_3-2_extract.py  --test-dir .
verifichiamo insieme se le estrazioni di test sono valide per capire se procedere con un batch di chunks maggiore. non modificare nessun file. non spiegarmi tutte le differenze, dammi solo le conclusioni e siamo pronti per un batch più grande o una full run oppure c'è un problema critico


# ANALISI METRICHE post Estrazione

Sei un analista che esamina il report diagnostico di un knowledge graph
biografico estratto da "Memorie di Adriano" di Yourcenar.
il report è @Adriano_graph/data/output/extraction_analysis/metrics.json 

CONTESTO. Il grafo è strutturato come indicato in
@Adriano_graph/src/schema.py , prompt di estrazione in @Adriano_graph/src/stage_3-1_prompt.py . Il grafo è stato estratto da 310 chunk del
testo, un chunk per chiamata LLM.

COSA DEVI FARE.
Analizza il report diagnostico in cerca di pattern problematici che
suggeriscono modifiche aggiuntive al prompt o allo schema. Focalizzati su:

1. SOTTO-ESTRAZIONE. Tipi di nodo o arco con frequenza sospettosamente
   bassa rispetto a quanto ci si aspetta da un testo biografico denso
   come Yourcenar. In particolare:
   - Place molto sotto-rappresentati? Quanti Event non hanno
     LOCATED_AT?
   - Theme rari (citati una sola volta): sono temi reali del testo
     che il modello ha colto distrattamente, o estrazioni spurie?
   - Reflection: distribuzione per chunk. Ce ne sono chunk con 0?
     Sono davvero chunk senza voce del narratore o sotto-estrazione?
   - Work: quanti? Si parla mai di edifici, libri, opere?

2. SOVRA-ESTRAZIONE / RUMORE. Pattern opposti:
   - RELATED_TO (jolly generico): se è frequente, il modello sta
     annacquando le relazioni invece di scegliere il tipo specifico.
   - Nodi con name molto generico (es. Theme "la vita", "il tempo")
     che probabilmente vanno consolidati o eliminati.
   - Event con grado anomalmente alto (>10 archi): possibili scene
     mal estratte, accorpamenti che andavano spezzati.
   - Confidence anomale: tutto a 0.9? Il modello non discrimina. Code
     basse (<0.4)?

3. ARCHI NARRATIVI. Lo stadio 3 ha enfasi sul cogliere la struttura
   narrativa, non solo i fatti.
   - ECHOES: quanti? Tra che tipi di Event? Sono coppie sensate?
     Pochi/zero ECHOES su 310 chunk sarebbe un campanello.
   - CAUSED vs FOLLOWS: rapporto? Tanto FOLLOWS e poco CAUSED dice
     che il modello legge il testo come cronaca anziché come
     narrazione causale.
   - CONTRASTS_WITH: quanti? Su che tipi? La regola del 0.3.0
     limitava ai casi marcati esplicitamente — è stata seguita?
   - TRANSFORMS_INTO: quanti? Su Phase o Person? Se zero o uno,
     il modello non sta cogliendo i passaggi interiori e di periodo.

4. EVENT INTERNI (regola nuova di 0.3.0). Quanti sono gli Event
   interni rispetto al totale degli Event? La soglia di ADR-015 era
   ≤15-20%. Sopra: la regola dei tre criteri non sta tenendo, (2)
   sta entrando come (3). Sotto il 5%: forse il modello non li sta
   estraendo per niente. Hanno aggancio CAUSED/FOLLOWS/DURING come
   richiesto?

5. QUALITÀ DELLA PROVENIENZA.
   - evidence_span sempre presenti?
   - confidence distribuita o appiattita?
   - chunk con zero nodi estratti?

6. DUPLICATI STRUTTURALI EVIDENTI (anticipazione stadio 4):
   - Person con stesso name ma id diverso.
   - Phase con name semanticamente equivalente ("giovinezza" vs
     "anni della giovinezza").
   - Theme con name simile.
   - Quanti casi ovvi ci sono?

OUTPUT. Per ogni pattern problematico che identifichi:
- nome del pattern
- evidenza nei dati (numeri concreti dal report)
- ipotesi di causa (limite del prompt? limite del modello?
  caratteristica del testo?)
- proposta di intervento: modifica al prompt, allo schema, o nessuna
  (a volte un pattern è caratteristica del testo, non un bug).

Sii sintetico. Vai dritto ai 5-10 pattern più rilevanti. Non
elencare metriche per amore di completezza: solo quelle che
suggeriscono un'azione.

nel report che hai fatto, l'analisi dei punti spiegala in modo comprensivo, il report deve accompagnare scelte anche importanti 
quindi voglio le motivazioni del perchè è un problema e in termini semplici, 
anche se sei più prolisso. immagina che parta da zero e devo poi riuscire a fare la stessa analisi da solo


Concludi con una raccomandazione finale: tra i pattern identificati,
ce ne sono di critici che necessitano di prevedere un prossimo bump di PROMPT_VERSION (costoso) e
quali sono problemi di stadio 4 (non di estrazione).

non modificare nessun file, se vuoi fare un file con il tuo report fallo in @Adriano_graph/data/output/extraction_analysis  e formato .md, se ti servono ulteriori info sul progetto li trovi in @Adriano_graph/PIPELINE.md 

