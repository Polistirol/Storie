# Analisi diagnostica del run di estrazione (PROMPT_VERSION 0.3.0)

> Input: `extracted_graph.json` del run `18-05-2026_16-51` su 310 chunk di
> *Memorie di Adriano*. Modello: `claude-sonnet-4-6`, temperature 0.0.
> Schema 0.1.0, prompt 0.3.0. 2960 nodi, 4621 archi (triple uniche),
> 0 chunk vuoti.

---

## Premessa metodologica: come si analizza un grafo estratto

Questa sezione esiste perché è il tuo primo lavoro sui KG e voglio che la
prossima volta tu possa farla da solo. Salta se vuoi andare diretto ai
pattern.

**Cosa stiamo cercando**. Non bug di codice, ma **pattern statistici** che
rivelano qualcosa sul *comportamento del modello*. L'estrazione è
stocastica anche a temperature 0 (perché il prompt cambia chunk per chunk
e i tipi di scelte sono molte), quindi non si valuta su un singolo chunk
ma su distribuzioni aggregate sui 310 chunk.

**Quattro lenti diagnostiche**. Quando guardi un report tipo questo,
usa quattro domande in ordine:

1. **Volume per tipo**: i conteggi `by_node_type` e `by_edge_type` sono
   plausibili rispetto al testo che hai dato in pasto? Su un libro
   biografico ti aspetti molti Event, Person e Place; molte Reflection
   (Yourcenar è un narratore meditativo); pochi Work (non è un saggio).
   Se un tipo è schiacciato a zero o esagerato, è segnale.
2. **Volume per arco**: gli archi narrativi (CAUSED, FOLLOWS, ECHOES,
   CONTRASTS_WITH, TRANSFORMS_INTO) sono quelli che dicono se il grafo è
   una *narrazione* o solo una *lista di fatti*. Se sono tutti a zero,
   il modello sta estraendo cronaca, non racconto.
3. **Distribuzioni di grado**. In ogni grafo reale i nodi hanno gradi
   molto disuguali: pochi hub centrali, una lunga coda di nodi a degree 1.
   Se la distribuzione è troppo piatta (tutti i Theme hanno 1-5
   occorrenze, nessuno svetta) il modello sta *frammentando*: non
   riconosce che due chunk parlano della stessa cosa. Se è troppo concentrata
   (un solo nodo che assorbe tutto) il modello sta *accorpando* troppo.
4. **Coerenza cross-chunk**. Lo stesso id usato per tipi diversi (vedi
   `warnings`) è un sintomo che il modello non discrimina bene fra
   categorie al confine (Event/Theme, Person/Place). I duplicati di nome
   (la stessa "Malattia terminale" come 5 nodi diversi) sono un sintomo
   diverso: il modello discrimina ma non sa che ne ha già visto uno.

**Convenzione che useremo per ogni pattern**:
- *Cosa ho guardato*: campo specifico del `metrics.json`.
- *Cosa misura*: una frase per spiegare la metrica.
- *Cos'è "normale"*: l'aspettativa qualitativa per un testo come Yourcenar.
- *I numeri reali*: evidenza dal report.
- *Perché è un problema (o non lo è)*: ragionamento esplicito.
- *Causa probabile*: limite del prompt, del modello, o del testo?
- *Intervento*: cosa cambiare, dove, e con che impatto atteso.

---

## Numeri di riferimento

Tenerli sotto mano per leggere i pattern.

**Nodi (2960 totali)**:
Person 295 · Event 840 · Place 314 · Phase 65 · Theme 497 · Reflection 855 · Work 94.

**Archi (4621 triple uniche)**:
INVOLVES 1321 · LOCATED_AT 512 · DURING 152 · CREATED 71 · RELATED_TO 83 · EMBODIES 596 · REFLECTS_ON 1444 · ECHOES 23 · CONTRASTS_WITH 47 · TRANSFORMS_INTO 2 · CAUSED 87 · FOLLOWS 283.

**Globali**: 310 chunk, 0 chunk vuoti, componente gigante 2703/2960 (91%),
17 warning di tipo discordante, Adriano degree 854 (oltre ogni altro nodo
di un ordine di grandezza).

---

## 1. Phase frammentata in cluster di sinonimi

> **Severità**: alta come segnale, ma è un problema di **stadio 4**, non
> di estrazione. NON modificare il prompt per questo.

### Cosa ho guardato

Sezione `phases.phase_event_count` del report (linea ~22555 di
`metrics.json`). Lista di tutte le 65 Phase con quanti Event le ancorano
via `DURING`. Cercavo nomi sospettosamente simili.

### Cosa misura

Quante Event diverse "abitano" ciascuna Phase. La Phase è pensata come
contenitore temporale di Event: se "Malattia terminale di Adriano" è una
fase reale, dovrebbe contenere decine di Event. Se invece la trovi
spezzata in più nodi con name quasi identico, ognuno ne contiene una
manciata, hai una frammentazione.

### Cos'è "normale"

In un testo biografico denso, una fase di vita centrale (giovinezza, 
guerra, malattia, lutto) dovrebbe contenere **almeno 10-30 Event ciascuna**,
con name unico canonico. Su 310 chunk e 840 Event, ti aspetti ~10-15 Phase
"forti" che assorbono la maggioranza degli Event.

### I numeri reali

La fase "Malattia terminale di Adriano" compare come **5 nodi distinti**:

| id | events_during | degree |
|---|---|---|
| `malattia_terminale` | 12 | 16 |
| `malattia_terminale_adriano` | 7 | 11 |
| `fase_malattia_terminale` | 6 | 7 |
| `malattia_terminale_di_adriano` | 4 | 4 |
| `vecchiaia_e_morte_imminente` | 0 | 1 |

Insieme ancorerebbero ~29 Event ma sono spezzati in 5 contenitori. Stesso
fenomeno per la giovinezza:
`giovinezza_di_adriano` (Italica), `giovinezza_ateniese`,
`giovinezza_militare_adriano` (Danubio), `giovinezza_romana_di_adriano`,
`formazione_giovanile` — 5 fasi distinte per quella che, come *Era della
vita*, è una sola.

### Perché è un problema

Tre conseguenze pratiche.

1. **Le query temporali falliscono**. Una domanda come "cosa accadde
   durante la malattia terminale di Adriano?" deve interrogare *una*
   Phase. Con 5 nodi, devi ricostruirli a mano o la query restituisce
   parzialmente sbagliato.
2. **Il grado dei nodi diventa fuorviante**. La Phase più importante della
   parte finale del libro non è in top 10 con degree 16, è in top 1 con
   degree ~40 — ma il grafo non lo sa.
3. **L'aggancio Event → Phase si indebolisce**. Quando vuoi calcolare
   "quanti Event mancano di Phase?" stai conteggiando contro Phase
   sbagliate. Le metriche di copertura mentono.

Sulla giovinezza c'è una sfumatura: le 5 sotto-fasi sono geograficamente
distinte (Italica, Atene, Danubio, Roma) e *come Phase emergenti dal
testo* hanno senso. Il problema è solo che *come Era* sono tutte
"gioventù". Questo è proprio il motivo per cui in 0.4.0 introduci Era
chiusa accanto a Phase: la Era unifica, le Phase restano come dettaglio.

### Causa probabile

Strutturale, non bug del prompt. Il modello vede **un solo chunk per
volta**. Nel chunk 23 si trova davanti la malattia, conia
`malattia_terminale`. Nel chunk 245 si trova davanti la malattia, non
ricorda quel name, conia `malattia_terminale_adriano`. Senza un dizionario
canonico iniettato cross-chunk, è inevitabile.

### Intervento

**Nessuno sul prompt di stadio 3**. Questo è esattamente il lavoro dello
stadio 4 (resolve): vedere tutti i nodi del grafo insieme e fondere
quelli che sono semanticamente lo stesso. Anzi: questi cluster sono il
*test case canonico* per progettare lo stadio 4. Quando lo stadio 4
sarà pronto, dovrà riconoscere queste 5 Phase come una sola.

Il piano 0.4.0 di introdurre Era chiusa allevia il danno operativo
(perché Era diventa la spina dorsale temporale e le Phase emergenti
diventano dettaglio secondario), ma non risolve la frammentazione delle
Phase emergenti.

---

## 2. Theme iper-frammentati con coda lunga

> **Severità**: alta. Intervento minore al prompt **consigliato per 0.4.0**.

### Cosa ho guardato

Sezione `themes.themes_ranked` (linea ~23310): tutti i 497 Theme ordinati
per `embodies_in` (quanti Event/Person incarnano quel Theme).
Poi `themes_degree_one` (linea ~26296): i Theme che compaiono una sola
volta in tutto il grafo.

### Cosa misura

`embodies_in` = quanti nodi diversi (Event o Person) hanno un arco
EMBODIES verso questo Theme. È la centralità tematica. Un Theme buono è
quello che riassume *una rete* di scene e persone, non *una singola scena*.

### Cos'è "normale"

In un libro tematicamente coerente come Yourcenar, ti aspetti una
piramide: 5-10 grandi Theme che incarnano dozzine di Event ("La morte",
"Il potere", "L'amore", "L'arte di governare", "Il tempo", "Il corpo"),
poi una coda di Theme più specifici. Il **top** dovrebbe avere
`embodies_in` di ordine 20-50, non 5.

### I numeri reali

- **497 Theme totali**. È più di un Theme e mezzo per chunk in media.
- **80 Theme con degree 1** (il 16%): compaiono una sola volta in tutto il
  libro, da una sola scena.
- **Il top di `embodies_in` è 5**. Otto Theme distinti hanno 5 o 4
  occorrenze. Nessuno svetta. La distribuzione è piatta a coda lunga.

Esempi di Theme degree-1 chiaramente sinonimi che il modello non ha
unificato:
- `bellezza_e_indifferenza`, `bellezza_e_sublime` (entrambi sulla
  bellezza estetica).
- `giovinezza_amore_e_natura`, `giovinezza_e_maturazione`.
- `amicizia_e_solidarieta_politica`, `amicizia_intellettuale`.
- `memoria_e_continuita_culturale_greca`, `immortalita_e_memoria`.
- `presagi_di_morte` accanto a `meditazione_sulla_morte` accanto a `morte`.

E altri molto specifici come "Antinoo come presenza divina e prodigio
per la folla" che sono in realtà aspetti di un Theme più generale
("Culto di Antinoo") già esistente nel grafo.

### Perché è un problema

Lo scopo del Theme è funzionare da **ponte concettuale** fra scene
distanti. La domanda "in quanti modi diversi Yourcenar parla della
morte?" si risponde *navigando* il Theme "morte" e seguendo i suoi
EMBODIES verso gli Event. Se la morte è frammentata in `morte`,
`meditazione_sulla_morte`, `presagi_di_morte`, `desiderio_di_morire`,
`confronto_con_i_morti`, `lutto_e_perdita`, ogni interrogazione coglie
solo una fetta.

In più, **la deduplicazione di Theme allo stadio 4 è il lavoro più duro
di tutto il progetto**. Person e Place hanno alias e nomi canonici
storici; Phase ha un set chiuso (con Era 0.4.0). I Theme no: sono inventati
dal modello chunk per chunk. Più sono numerosi e specifici, più diventa
impossibile fonderli automaticamente.

### Causa probabile

La regola 0.2.0 "Theme incarnato" è un successo a metà: il modello *trova*
i temi anche dove non sono lessicalizzati (volume alto, buono), ma ogni
volta li *nomina* a misura del chunk corrente ("Antinoo come presenza
divina e prodigio"). Nessuna parte del prompt 0.3.0 chiede di
generalizzare il name o di riusare un name già visto. Il modello fa
quello che fa naturalmente: descrive in dettaglio.

### Intervento

**Aggiungere al SYSTEM_PROMPT una regola di canonicità del name del
Theme**. Suggerimento di formulazione:

> Il `name` di un Theme è un sintagma breve e generale (≤3 parole
> sostantive), pensato per essere riusato attraverso il libro: "la
> morte", "il lutto", "memoria e immortalità", "decoro pubblico",
> "successione". La specificità del Theme nel chunk corrente va in
> `description`, non in `name`. Se due possibili name del Theme
> differiscono solo per dettagli ("bellezza", "bellezza e indifferenza
> a se stessa", "bellezza e sublime"), scegli il più breve e generale.

Non risolve la deduplicazione (è stadio 4), ma **comprime la cardinalità
attesa** dei Theme da ~500 a ~150-200, e rende il merge a valle
fattibile. È poco invasivo: 4-5 righe nel prompt + un esempio nei
few-shot.

---

## 3. ECHOES quasi assenti

> **Severità**: alta come metrica, ma è un problema di **stadio 6**, non
> di stadio 3.

### Cosa ho guardato

Sezione `narrative_arcs.counts.ECHOES` = 23. Sezione
`narrative_arcs.echoes_reciprocal_pairs_count` = 0.

### Cosa misura

ECHOES è l'arco Event → Event che dice "questi due eventi rispecchiano
uno l'altro narrativamente": una scena che richiama un'altra scena lontana
nel testo. Le coppie reciproche sono quelle dove sia A→B che B→A esistono,
segno che il modello le ha viste come davvero parallele.

### Cos'è "normale"

*Memorie di Adriano* è un libro **ecoico per definizione**: tutto il libro
è un Adriano vecchio che torna mentalmente su scene precedenti, profezie
che si compiono, luoghi rivisitati, paralleli fra il proprio destino e
quello di altri. Su 310 chunk e 840 Event, ti aspetti centinaia di
ECHOES, non 23.

### I numeri reali

- **23 ECHOES** in tutto il grafo (0.07 per chunk, 1 ECHOES ogni 36 Event).
- **0 coppie reciproche**: nessun caso in cui il modello, vedendo A,
  abbia anche stabilito che B richiama A.

### Perché è un problema, ma soprattutto: perché non si risolve nel prompt

Pensa al meccanismo: per estrarre `ECHOES(morte_di_antinoo,
morte_di_traiano)`, il modello deve vedere *entrambe* le morti.
Ma vede un chunk alla volta. Nel chunk dove muore Antinoo, il chunk dove
muore Traiano è 100 chunk indietro e non è nel contesto. Il modello
non ha *strutturalmente* modo di estrarre questo arco.

ECHOES funziona solo per echi *intra-chunk* (due scene parallele dentro
lo stesso paragrafo, raro) o quando il chunk corrente nomina
esplicitamente la scena lontana ("come allora a Cnido, anche qui..."),
che è il caso che genera quei 23.

**Quindi**: il numero 23 non misura il fallimento dell'estrazione, misura
**il limite intrinseco dell'estrazione mono-chunk** rispetto agli archi
cross-chunk.

### Intervento

Due strade.

- **(a) Accettare il limite** e demandare gli ECHOES allo **stadio 6
  (enrich)**, un passaggio LLM dedicato che vede gruppi di chunk insieme
  e propone ECHOES cross-chunk. È coerente con la pipeline (ADR-009: ogni
  stadio fa una cosa).
- **(b) Iniettare contesto** nel prompt di stadio 3: per ogni chunk,
  passare al modello la lista (id, name) degli Event già estratti dai
  chunk precedenti della stessa parte. Cattura ECHOES che attraversano
  brevi distanze, ma rompe parzialmente l'idempotenza mono-chunk
  (l'output del chunk N dipende dal chunk N-1) e aumenta i token.

**Raccomandazione**: (a). Non toccare il prompt di stadio 3. Lasciare
ECHOES come compito esplicito di stadio 6, dove ha la struttura per
funzionare.

---

## 4. CAUSED << FOLLOWS: il modello legge cronaca, non narrazione

> **Severità**: alta. **Intervento consigliato in 0.4.0**.

### Cosa ho guardato

Sezione `narrative_arcs.counts.CAUSED` = 87, `FOLLOWS` = 283, e il
`caused_follows_ratio` = 0.31 calcolato per comodità.

### Cosa misura

- `FOLLOWS(A, B)` = B viene cronologicamente dopo A, senza pretesa di
  spiegare *perché*.
- `CAUSED(A, B)` = A è causa o motivo narrativo di B. Implica
  un'interpretazione del legame, non solo una sequenza.

Il rapporto CAUSED/FOLLOWS dice **come il modello legge il testo**:
- Rapporto basso (≪1) → cronaca: enumera fatti in sequenza.
- Rapporto alto (≫1) → narrazione: ogni successione è una conseguenza.
- Rapporto bilanciato (~0.5-1) → letto come testo letterario con causalità
  esplicita marcata.

### Cos'è "normale"

Yourcenar **non scrive cronaca**. Quasi ogni successione di Event nelle
*Memorie* è interpretata: Adriano fa X "perché" o "ormai" o "grazie a" Y;
una scelta politica è conseguenza di un'esperienza interiore; un viaggio
nasce da un lutto. Un rapporto CAUSED/FOLLOWS atteso su Yourcenar è
tra 0.7 e 1.5. Il valore 0.31 significa che per ogni nesso causale il
modello sta estraendo 3.25 FOLLOWS.

### I numeri reali

- **CAUSED = 87** (1.9% degli archi totali, 0.28 per chunk).
- **FOLLOWS = 283** (6.1% degli archi, 0.91 per chunk).
- **Rapporto = 0.31**.

### Perché è un problema

L'obiettivo finale del progetto è un agente conversazionale che parla in
prima persona come Adriano. Le domande tipiche a quell'agente saranno:
*"Perché hai adottato Antinoo a culto?"*, *"Cosa ti spinse a costruire
il Vallo in Britannia?"*, *"Da cosa nacque il tuo lutto?"*. Tutte
**richieste causali**. Se il grafo non ha CAUSED esplicito, l'agente
deve ricostruirli a runtime dal solo testo, perdendo gran parte del valore
del knowledge graph.

Pensa al grafo come a un *modello compresso del testo*: se la
compressione butta via la causalità e tiene solo la cronologia,
hai compresso male.

### Causa probabile

Il prompt 0.3.0 menziona CAUSED solo nel contesto dell'aggancio
obbligatorio dell'Event interno (ADR-015):

> "(1) CAUSED se il testo marca il nesso, anche solo via 'dopo che/
> perché/ormai/grazie a' o per evidente implicazione narrativa"

Ma questo è **solo per gli Event interni**. Per gli Event esterni
(che sono la maggioranza), CAUSED non è raccomandato sopra FOLLOWS.
Il modello, senza pressione esplicita, sceglie l'opzione "più sicura"
e meno interpretativa: la cronologia.

### Intervento

Aggiungere una **regola generale di priorità** nel SYSTEM_PROMPT,
sezione archi narrativi:

> Tra due Event in successione temporale, **preferisci CAUSED** quando
> il testo suggerisce un nesso narrativo: connettori espliciti ("dopo
> che", "perché", "ormai", "il che", "per questo", "grazie a"),
> conseguenza politica/militare ("dopo la vittoria, ottenne…"),
> reazione psicologica/decisionale ("alla notizia, decise di…"). Riserva
> `FOLLOWS` a successioni puramente cronologiche: date, consolati,
> tappe enumerate di un viaggio, sequenze di anni senza nesso narrativo
> esplicito.

Costo: 4-6 righe nel prompt + idealmente un esempio nei few-shot. Impatto
atteso: inversione del rapporto a favore di CAUSED, **da 0.31 a
qualcosa attorno a 0.8-1.2**.

---

## 5. TRANSFORMS_INTO ≈ 0: nessuna trasformazione interiore catturata

> **Severità**: alta come segnale, ma in larga parte risolto dal piano
> 0.4.0 senza intervento aggiuntivo sul prompt.

### Cosa ho guardato

Sezione `narrative_arcs.counts.TRANSFORMS_INTO` = 2.
Sezione `phases.transforms_chains` = una sola catena di 2 elementi:
"Fase della molteplicità interiore" → "Fase dell'emergenza del regista
interiore".

### Cosa misura

TRANSFORMS_INTO è l'arco che dice "X cambia *diventando* Y". Lo schema
lo ammette su:
- Phase → Phase (passaggio di fase di vita: gioventù → adultità)
- Person → Person (cambiamento interiore di una persona: l'Adriano-soldato
  diventa l'Adriano-imperatore)

È l'arco specifico della **biografia interiore**, e in *Memorie* è
ovunque: Adriano si trasforma continuamente, e il libro lo dice
esplicitamente.

### Cos'è "normale"

Su 310 chunk di un libro che descrive 60 anni di vita interiore, ti
aspetti almeno una decina di TRANSFORMS_INTO Phase→Phase (i passaggi
maggiori della vita) e svariati Person→Person (Adriano in diversi
momenti di sé, ma anche Antinoo da efebo a divinità, ecc.).

### I numeri reali

- **TRANSFORMS_INTO totali = 2**.
- **Phase→Phase = 1** (una catena di 2).
- **Person→Person = 1** (presumibilmente).

### Perché è un problema, e perché 0.4.0 ne risolve la maggior parte

Stessa dinamica strutturale di ECHOES: per dire "gioventù → adultità",
il modello dovrebbe vedere entrambe le fasi insieme. Vede solo un chunk.

**Ma** con 0.4.0 questo si risolve quasi gratis:
- Era è un **set chiuso** di 4 valori (infanzia, gioventù, adultità,
  vecchiaia).
- L'ordine è deterministico.
- Quindi le 3 catene TRANSFORMS_INTO fra Era (infanzia→gioventù,
  gioventù→adultità, adultità→vecchiaia) possono essere generate
  **automaticamente dal codice**, non chieste al modello.

Resta scoperto:
- TRANSFORMS_INTO fra **Phase emergenti** (passaggi intermedi).
- TRANSFORMS_INTO fra **Person→Person** (cambiamento interiore di un
  personaggio).

Entrambi richiedono visione cross-chunk e vanno demandati allo
**stadio 6** (enrich).

### Intervento

In 0.4.0:
1. Niente regola TRANSFORMS_INTO nel prompt.
2. Nel codice di stadio 3.5 (caricamento Neo4j), generare deterministicamente
   le TRANSFORMS_INTO Era→Era.
3. Demandare il resto a stadio 6.

---

## 6. Event privi di Phase: 82% (già coperto dal piano 0.4.0)

> **Severità**: altissima ma **già nel piano 0.4.0**. Pattern incluso solo
> per quantificare il problema.

### Cosa ho guardato

Sezione `event_quality.missing_phase_count` = 689 (su `event_total` = 840).
Sezione `phases.top_events_without_during` (Event hub senza alcuna Phase
collegata).

### Cosa misura

DURING è l'arco Event → Phase che colloca un Event in una fase di vita.
È l'**ancoraggio temporale primario** del grafo: senza Phase, l'Event
"galleggia" e non sai quando colocarlo nel tempo della vita di Adriano.

### Cos'è "normale"

In una biografia, **quasi ogni Event ha un quando**. La regola
"Event → Phase quando il testo lo permette" dovrebbe coprire l'80%+ degli
Event. Una quota residua del 10-20% senza Phase è accettabile (Event
"fluttuanti", aforismi, scene non datate).

### I numeri reali

- **689 Event su 840 senza DURING (82%)**.
- Solo 152 archi DURING in totale → rapporto DURING/Event = 0.18.
- Mancano DURING anche su Event di alto grado (centrali per il libro):
  `morte_di_antinoo` (degree 24), `diffusione_culto_antinoo` (14),
  `avvenimenti_oscuri_agonia_traiano` (12), `esecuzioni_multiple_nemici`
  (12).

### Perché è un problema

Senza Phase, **il grafo è privo di asse temporale**. Le query
"durante la malattia, cosa accadde?" o "negli anni della giovinezza
militare, dove era?" non funzionano. È, di nuovo, un punto centrale per
l'agente conversazionale: ogni risposta in prima persona di Adriano è
implicitamente datata (sto raccontando ora *come* anziano *di quando*
ero giovane), e il grafo deve poterlo rappresentare.

### Causa probabile

Doppia.

1. Il prompt 0.3.0 richiede DURING **solo per Event interno** (come
   fallback se manca un Event esterno-ancora). Per gli Event esterni,
   DURING è opzionale, e il modello quasi sempre lo omette.
2. Le Phase sono frammentate (Pattern 1): anche dove il modello volesse
   ancorare un Event, non ha sempre un id stabile per quella Phase
   attraverso i chunk.

### Intervento

**Niente da aggiungere** rispetto al piano 0.4.0. Le due decisioni
- Introduzione di Era come spina dorsale,
- Ancoraggio obbligatorio Event → Phase (o Era) quando il testo lo
  permette,

risolvono direttamente questo pattern. Era ha il vantaggio cruciale di
essere a **set chiuso**: l'aggancio Event → Era è quasi sempre fattibile
("questo Event della giovinezza di Adriano in Britannia → Era=gioventù"),
mentre l'aggancio Event → Phase emergente resta fragile finché lo
stadio 4 non deduplica le Phase.

**Nota operativa**: dopo 0.4.0, il numero da monitorare non è più
`missing_phase_count` ma `missing_era_count` (da aggiungere a
`extraction_analysis.py`). Atteso: ≤10-20%.

---

## 7. Discordanza di tipo per stesso `id` attraverso chunk diversi

> **Severità**: media-alta. **Intervento consigliato in 0.4.0**, costo
> basso, impatto alto.

### Cosa ho guardato

Sezione `warnings` in fondo al `metrics.json`. 17 warning, ciascuno di
forma "Tipo discordante per id=X: T1 (in ch_NNNN) vs T2 (in ch_MMMM)".

### Cosa misura

Quando il modello, in due chunk diversi, assegna allo *stesso id
canonico* due *tipi di nodo diversi*. Questo è diagnostico
dell'**indecisione del modello al confine fra categorie semantiche**:
non sa se "morte di Antinoo" è un Event accaduto, un Theme che ricorre,
o una Phase di vita.

### Cos'è "normale"

Zero, o quasi. Una manciata di casi su 2960 nodi è inevitabile (id
generati a parità di name su entità ambigue tipo "Eufrate" il fiume vs
"Eufrate" il re arsacide). 17 casi su 2960 è poco in proporzione ma
contengono casi *importanti*, non solo strafalcioni periferici.

### I numeri reali

17 warning. Li raggruppo per pattern semantico.

**Event ↔ Theme (su grandi eventi tematizzati)**:
- `morte_di_antinoo`: Event (ch_0183) vs **Theme** (ch_0223). L'evento
  centrale del libro, considerato a volte come fatto, a volte come tema
  ricorrente.
- `matrimonio_adriano_sabina`: Event (ch_0057) vs Theme (ch_0180) vs
  **Phase** (ch_0273). Tre tipi diversi per lo stesso matrimonio!
- `morte_di_adriano`: Theme (ch_0002) vs Phase (ch_0310). Idem.
- `culto_di_antinoo`: Work (ch_0217) vs Theme (ch_0302).

**Person ↔ Place (su istituzioni)**:
- `senato`: Person (ch_0100) vs Place (in 5+ chunk). Il Senato come attore
  collettivo vs il Senato come luogo fisico.

**Event ↔ Reflection (su scene introspettive)**:
- `primo_incontro_con_antinoo`: Reflection (ch_0159) vs Event (ch_0160).

**Work ↔ Place (su fondazioni urbanistiche)**:
- `colosseo`: Work (ch_0176) vs Place (ch_0218).
- `aelia_capitolina`: Work (ch_0195) vs Place (ch_0249, ch_0264).

**Ambiguità storica autentica**:
- `eufrate`: Place vs Person (probabilmente fiume vs Re arsacide
  omonimo).
- `boristene`: Person vs Place (Boristene il cavallo vs Boristene il
  fiume).

### Perché è un problema

Quando lo stesso id punta a due tipi, in stadio 4 (resolve) dovrai
**scegliere** un tipo "vero", scartando le occorrenze sull'altro tipo.
Significa che alcune estrazioni vengono di fatto cancellate. E lo stadio
4 non sa quale sia il tipo "giusto": deve interrogare il testo
nuovamente, raddoppiando il lavoro.

Peggio: il caso "matrimonio_adriano_sabina" Event/Theme/Phase rivela una
**confusione concettuale** del modello su cosa sono Event vs Theme vs
Phase per gli eventi storici nominali (eventi singoli con nome canonico:
"Matrimonio con Sabina", "Morte di Antinoo", "Adozione di Lucio").
Questa confusione non è solo nell'id collisione: è probabile che in
**molti chunk dove non c'è collisione**, il modello stia comunque
sbagliando il typing.

### Causa probabile

Il prompt 0.3.0 distingue bene Event vs Reflection (è il caso curato), e
in 0.2.0 ha chiarito i casi di scena vs atto politico, ma **non
disambigua esplicitamente i casi al confine Event/Theme/Phase per eventi
nominali**, né Istituzione-come-Person vs Istituzione-come-Place, né
Work-vs-Place per fondazioni edilizie. Il modello sceglie chunk per
chunk in base al contesto immediato.

### Intervento

Aggiungere al SYSTEM_PROMPT una **regola di disambiguazione del typing**
in 3 punti:

> **Eventi storici nominali**: ogni evento puntuale della biografia che
> ha un nome canonico ("Morte di Antinoo", "Matrimonio con Sabina",
> "Adozione di Lucio", "Battaglia X") è **sempre Event**, anche quando
> il chunk corrente lo richiama come tema ricorrente o come momento
> spartiacque di una fase. La sua dimensione tematica può comparire come
> Theme **separato** (es. `lutto_per_antinoo` Theme accanto a
> `morte_di_antinoo` Event), con EMBODIES dall'Event al Theme.

> **Istituzioni come attori collettivi**: Senato, Curia, Pretorianato,
> città-stato che agiscono o decidono ("il Senato decretò") sono
> **Person**. La loro sede fisica è collegata via LOCATED_AT su un Place
> separato ("Roma"). Mai Place direttamente.

> **Fondazioni edilizie**: se il chunk parla **dell'atto di fondare o
> costruire** (Antinopoli, Aelia Capitolina, Villa Adriana, il Pantheon
> ricostruito), è **Event**. Se parla **dell'oggetto come luogo**, è
> **Place**. Mai Work. Work è riservato a opere mobili: libri, ritratti,
> statue, manoscritti, leggi codificate, monete.

Costo: 8-10 righe nel prompt. Impatto: rimuove ~15 dei 17 warning
e — molto più importante — previene tutte le sbavature analoghe che non
arrivano alla soglia del warning (collisione di id) ma esistono lo stesso.

---

## 8. Confidence appiattita a 0.85–0.95

> **Severità**: bassa. Intervento opzionale, non bloccante.

### Cosa ho guardato

Sezione `provenance.node_confidence`:
- mediana 0.9, mean 0.91
- p10 = 0.85, p90 = 0.95, p25 = 0.9, p75 = 0.95
- min 0.6, max 0.99
- below 0.7 = 4 occorrenze su 4271.

Sezione `provenance.edge_confidence`:
- mediana 0.9, mean 0.90
- p10 = 0.8, p90 = 0.95
- min 0.4 (un solo arco), max 0.99
- below 0.5 = 1 arco.

### Cosa misura

`confidence` è il valore che il modello assegna a se stesso quando
estrae un nodo o un arco, dichiarando quanto è sicuro della scelta.

### Cos'è "normale"

Una confidence *informativa* dovrebbe distribuirsi: alcuni 0.95
("sono sicurissimo, il testo lo dice esplicitamente"), alcuni 0.6
("è un'inferenza, plausibile ma non scritta"), pochi 0.4 ("forzatura,
da rivedere"). La distribuzione utile ha p10 ≤ 0.6 e p90 ≥ 0.95.

### I numeri reali

- **Il 50% dei nodi è esattamente a 0.9** (p25=0.9, p75=0.95).
- Solo **4 nodi su 4271 sotto 0.7**.
- Quasi **zero discriminazione**: il modello quasi non usa il range basso.

### Perché è un (modesto) problema

L'idea originale della confidence è che lo stadio 5 (validate) la usi per
flaggare nodi/archi a confidence bassa come candidati a revisione umana.
Se tutto è 0.9, il segnale è inutile: non sai cosa rivedere prima.

**Non è un problema strutturale**, solo un'occasione persa. Era già noto
dalla letteratura LLM (e documentato in `PIPELINE.md` stadio 3) che
l'auto-stima dei modelli è imprecisa.

### Causa probabile

Bias dei modelli istruiti: senza ancore esempi che mostrano quando dire
0.5 o 0.6, ripiegano sul valore "rassicurante" 0.85-0.95.

### Intervento

Due opzioni, entrambe accettabili.

- **(a) Rimuovere `confidence` dall'output del modello**. La
  reintroduci in stadio 5 come `human_validated`. Risparmia token,
  smette di raccogliere rumore.
- **(b) Lasciarla** e aggiungere ai few-shot un esempio con
  `confidence = 0.55` su un'estrazione genuinamente dubbia
  (Theme inferito senza esplicitazione, arco CAUSED su nesso solo
  implicito), per insegnare il range basso.

Raccomandazione: (b). Costo zero per il prompt (un esempio già
esiste, basta marcare un'estrazione come `confidence: 0.55`), e mantiene
l'opzione del segnale.

**Non bloccante per 0.4.0**.

---

## 9. Person duplicate per id

> **Severità**: media. Problema di **stadio 4**, non di estrazione.

### Cosa ho guardato

Sezione `hubs.degree_one_person`: 99 Person con grado 1. Ho cercato fra
queste 99 nomi simili a hub già esistenti.

### Cosa misura

Person che appaiono una sola volta nel grafo. Una parte sono Person
*davvero* periferiche (citati una volta nel libro). Un'altra parte sono
**duplicati cammuffati**: lo stesso personaggio già presente con un id
diverso, ma ri-citato con name leggermente diverso che ha generato un
secondo id.

### Cos'è "normale"

In un libro come Yourcenar, ti aspetti **molte Person a degree 1**: nomi
citati di sfuggita, antenati, personaggi storici di contorno. Ma quei
nomi devono *davvero* essere periferici, non hub mascherati.

### I numeri reali

99 Person a degree 1 su 295 totali (33%). Sospetti evidenti dal report:

| Hub | Duplicato a degree 1 |
|---|---|
| `arriano` (degree 26, "Arriano di Nicomedia") | `arriano_di_nicomedia` (degree 1, "Arriano di Nicomedia") |
| `antonino` (degree 15, "Antonino") | `antonino_pio` (degree 1, "Antonino Pio") |

E con buona probabilità altri casi nascosti tra le 99 entry.

### Perché è un problema

Le query "tutto quello che riguarda Arriano" devono interrogare un
nodo unico. Con due nodi `arriano` e `arriano_di_nicomedia` separati, le
informazioni del secondo sono *invisibili* da chi parte dal primo.
Inoltre il calcolo del grado è sbagliato: Arriano dovrebbe avere
degree 27, non 26.

### Causa probabile

Stessa di Pattern 1: id generato dal modello chunk per chunk a partire
dal name che compare nel testo. La prima occorrenza è "Arriano" → id
`arriano`. Una occorrenza successiva è "Arriano di Nicomedia" → id
`arriano_di_nicomedia`. Inevitabile senza un dizionario canonico
cross-chunk.

### Intervento

**Nessuno sul prompt di stadio 3**. Lo stadio 4 deve fondere questi
duplicati. Una soluzione possibile per stadio 4: data una Person, lista
gli alias visti in tutti i chunk; se l'alias di una è il name di
un'altra, fondi.

Una via alternativa più sofisticata: iniettare un *glossario canonico*
nel prompt di stadio 3, con i nomi italiani canonici di tutti i
personaggi storici principali. Ma sarebbe un embrione di resolve dentro
l'extract, che la pipeline (ADR-009) tiene esplicitamente separati. **Non
raccomandato per 0.4.0**: meglio tenere l'architettura pulita.

---

## 10. REFLECTS_ON dominato dai Theme

> **Severità**: nessuna. È **caratteristica del testo**, non un bug. Lo
> inserisco perché è una scoperta da tenere a mente per gli stadi
> successivi.

### Cosa ho guardato

Sezione `reflections.reflects_on_target_types`:

| target | count |
|---|---|
| Theme | 631 |
| Event | 370 |
| Person | 321 |
| Place | 53 |
| Phase | 47 |
| Work | 15 |
| Reflection | 7 |

Totale archi REFLECTS_ON = 1444 (somma).

### Cosa misura

REFLECTS_ON è l'arco Reflection → qualsiasi-nodo: dice "questa
riflessione del narratore commenta/parla di questo nodo". La
distribuzione per tipo di target ti dice **su cosa Adriano riflette di
più**.

### I numeri reali

- Il **44% delle Reflection commentano un Theme**.
- Insieme, Theme + Event + Person coprono il 91%.
- Phase, Place, Work sono target marginali.

### Perché non è un problema

Yourcenar **riflette per concetti, non per fatti**. Adriano non
commenta "questo evento specifico", commenta "la morte come tale",
"il tempo", "il potere", "la giovinezza". La forte preponderanza di
REFLECTS_ON → Theme è il riflesso fedele dello stile della Storoni
Mazzolani / Yourcenar, non una sbavatura.

Combinato con la regola 0.2.0 "Theme incarnato" (che aumenta il volume
di Theme), si crea un asse Reflection → Theme molto denso. Il grafo sta
catturando la dimensione *meditativa* del libro: bene.

### Intervento

**Nessuno** sull'estrazione. Da ricordare in due punti futuri.

- **Stadio 4 (resolve)**: la deduplicazione dei Theme è ancora più
  critica di quanto sembri, perché ogni Theme è un nodo "attraversato"
  da molte Reflection.
- **Stadio 7 (RAG ibrido)**: la query "cosa pensa Adriano di X" passa
  per Reflection → REFLECTS_ON → Theme molto più che per Event o Person.
  L'indice deve essere ottimizzato per questo pattern di traversal.

---

## Note collaterali (non patternizzate, ma utili)

- **Reflection per chunk**: media 2.76, solo 9 chunk con zero Reflection
  (3%). Bilancio sano. La regola 0.3.0 di tripartizione (gnomica /
  retrospettiva / Event interno) non ha compresso troppo le Reflection.
- **Event interni**: non sono distinguibili dagli Event esterni in
  questo report (il campo `internal` non esiste). La soglia ≤15-20%
  di ADR-015 non è misurabile da `metrics.json` allo stato attuale.
  **Suggerimento operativo per il prossimo run**: aggiungere a
  `extraction_analysis.py` un'euristica di riconoscimento Event interni
  basata sul name (occorrenze di "calma", "presa di coscienza",
  "ricordo improvviso", "capriccio", "stato d'animo") o, meglio,
  un flag esplicito in `schema.py` da chiedere al modello (`is_internal:
  bool`).
- **RELATED_TO 83**: 1.8% del totale archi, 0.27 per chunk. È **basso**:
  il modello non sta usando RELATED_TO come jolly pigro temuto. La 0.3.0
  sta tenendo su questo punto.
- **CONTRASTS_WITH 47**: ragionevole. La regola 0.2.0 "solo se marcato
  esplicitamente dal testo" sta tenendo: niente over-extraction
  speculativa.
- **Componente gigante 2703/2960 (91%)**: connettività sana. Gli 183
  nodi isolati e i 204 componenti totali sono per lo più conseguenza
  della frammentazione (id non riusati attraverso chunk). Si risolvono
  in stadio 4.
- **Event solo-Adriano**: 328 su 840 (39%). Già coperto dalla decisione
  0.4.0 di marcare Adriano come `:Subject` e iniettarne il profilo nel
  prompt invece di estrarlo come nodo per ogni chunk.
- **Work 94, CREATED 71**: rapporto 0.76 (76% dei Work ha un creatore
  esplicito). Numericamente plausibile. Yourcenar parla molto di edifici
  (Villa Adriana, Pantheon, Olimpieion) e meno di opere proprie. Non è
  sotto-estrazione palese.
- **Place 314**: numericamente sano. Top hub (Roma 60, Atene 32, Villa
  Adriana 22, Antiochia 17, Gerusalemme 16) corrispondono ai luoghi
  reali del libro. Place sembra il tipo più "in salute" del grafo.

---

## Raccomandazione finale

### Da aggiungere a PROMPT_VERSION 0.4.0 (oltre alle 4 decisioni già prese)

Tre interventi al prompt, tutti poco invasivi, alto rendimento atteso.

1. **Disambiguazione del typing (Pattern 7)**. Glossa di 8-10 righe nel
   SYSTEM_PROMPT che fissa: Eventi storici nominali → sempre Event;
   Istituzioni come attori → Person; Fondazioni edilizie atto/luogo
   → Event/Place rispettivamente.
   **Costo**: minimo (~10 righe + cache invalidata una volta).
   **Impatto atteso**: risolve ~15 dei 17 warning di tipo discordante e
   previene molti id-collision per il merge di stadio 4.

2. **Priorità CAUSED sopra FOLLOWS (Pattern 4)**. Regola che inverte la
   preferenza del modello: usa CAUSED quando c'è nesso narrativo
   esplicito o implicito, riserva FOLLOWS a cronologia pura.
   Aggiungere un esempio nei few-shot.
   **Costo**: basso (~6 righe nel prompt, 1 modifica ai few-shot).
   **Impatto atteso**: inversione del rapporto CAUSED/FOLLOWS da 0.31
   verso 0.8-1.2. Trasforma il grafo da cronaca a narrazione causale,
   che è uno degli obiettivi dichiarati dello stadio 3.

3. **Canonicità del name del Theme (Pattern 2)**. Regola: `name` del
   Theme = sintagma breve (≤3 parole sostantive), generale, riusabile;
   la specificità del chunk va in `description`.
   **Costo**: minimo (~4 righe + esempio).
   **Impatto atteso**: comprime la cardinalità dei Theme da 497 verso
   ~200, rende fattibile la deduplicazione tematica in stadio 4.
   Senza questa regola, lo stadio 4 sui Theme sarà un incubo.

### Già coperti dalle 4 decisioni del piano 0.4.0

Da **non** ri-affrontare nel prompt, sono già nel piano.

- **Pattern 6 (Event senza DURING)** → coperto da "ancoraggio obbligatorio
  Event → Phase" + Era come backbone temporale.
- **Pattern 5 (TRANSFORMS_INTO Phase ≈ 0)** → parzialmente coperto. Le
  catene TRANSFORMS_INTO fra Era diventano deterministiche via codice in
  stadio 3.5, non chieste al modello.
- **Eventi solo-Adriano (328)** → coperto da Adriano marcato `:Subject` e
  profilo iniettato nel prompt; gli Event di Adriano-solo non saranno più
  necessari.
- **INVOLVES con ruoli (protagonist/participant/mentioned)** → cambia
  la semantica del conteggio "only_adriano" e riduce il rumore degli
  INVOLVES su nomi citati di sfuggita.

### Problemi di stadio 4 (resolve), NON di estrazione

Non toccare il prompt. Sono il *materiale di lavoro* dello stadio 4.

- **Pattern 1 (Phase frammentata)**: 5 varianti di "Malattia terminale di
  Adriano", 4-5 varianti di "Giovinezza". Caso da manuale del resolve.
- **Pattern 9 (Person duplicate per id)**: `arriano` vs
  `arriano_di_nicomedia`, `antonino` vs `antonino_pio`. Probabilmente
  altri tra i 99 Person a degree 1.
- **Deduplicazione Theme**: anche con la regola di canonicità del
  Pattern 2 attiva da 0.4.0, lo stadio 4 dovrà comunque consolidare
  Theme rimanenti. La regola riduce il carico, non lo elimina.

### Problemi di stadio 6 (enrich cross-chunk)

Limiti intrinseci dell'estrazione mono-chunk. Non forzare nel prompt.

- **Pattern 3 (ECHOES ≈ 23)**: limite strutturale. Va affrontato con un
  passaggio LLM dedicato cross-chunk in stadio 6.
- **TRANSFORMS_INTO Person→Person**: stesso discorso. Il cambiamento
  interiore di Adriano fra fasi della vita è intrinsecamente cross-chunk.

### Non agire

- **Pattern 8 (confidence appiattita)**: rumore noto, non bloccante.
  Eventualmente intervenire con un esempio few-shot a confidence bassa,
  ma è opzionale.
- **Pattern 10 (Reflection → Theme dominante)**: caratteristica del
  testo. Da supportare nel design dello stadio 7 (RAG), non corretto in
  estrazione.

---

## Sintesi in una riga (per la cima del prossimo ADR)

Il modello sta facendo bene la grana scena (0.2.0) e gli Event interni
(0.3.0); male la causalità narrativa, la canonicità degli identificativi
e la generalità dei name dei Theme. Le tre regole nuove proposte per
0.4.0 (typing disambiguation, CAUSED > FOLLOWS, name canonico del
Theme) sono poco invasive e ad alto rendimento. Le grandi questioni di
deduplicazione (Phase, Person, Theme) e di archi cross-chunk (ECHOES,
TRANSFORMS_INTO) vanno tenute fuori dallo stadio 3 e mantenute come
responsabilità degli stadi 4 e 6.
