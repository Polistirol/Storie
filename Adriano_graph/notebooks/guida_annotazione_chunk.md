# Guida passo-passo all'annotazione di un chunk

> Documento di lavoro. Aggiornare quando il metodo si raffina.
> Allineata a `PROMPT_VERSION 0.2.0` (vedi ADR-014 in `Adriano_graph/PIPELINE.md`).
> Ultimo aggiornamento: grana scena vs fotogramma, Theme incarnato, contrasti espliciti, antefatti in subordinata, regola di densità.

---

## Stato attuale del prototipo Stadio 3

- **Pila A (esempi few-shot)**: 4 chunk selezionati e annotati a mano.
- **Pila B (test gold-standard)**: 4 chunk selezionati e annotati a mano, non visti dal modello.
- **Pila C (casebook di casi-limite)**: in costruzione. Vi confluiscono i chunk troppo ambigui per essere esempi e i casi su cui le due chat di confronto hanno divergato.

**Cautele aperte** (da affrontare nella chat di implementazione):

1. **Token budget**: ~~rischio di superare 6-7k token~~ → risolto dal prompt caching a due breakpoint (ADR-013). Il prompt fisso misurato è ~14k token (SYSTEM_PROMPT ~1.7k + tool ~0.5k + 4 esempi ~11.8k); pagato pieno solo alla prima chiamata, letto da cache a ~10% del costo dalle successive (entro TTL ~5 min, sliding). Cautela conservata in forma debole: non aggiungere esempi finché non si misura di nuovo l'impatto.
2. **Test sotto-dimensionati**: 4 chunk di test sono sufficienti per validare qualitativamente il prompt nel prototipo, non per la valutazione finale. Debito tecnico: estendere a 10-15 chunk di test, distribuiti su tutte e sei le parti del libro, prima del run completo.
3. **Divergenze tra le due chat di annotazione**: ogni divergenza è un caso da mettere nel casebook. Significa che il contratto ha ancora ambiguità, e quelle ambiguità riemergeranno con l'estrattore.
4. **Riallineamento Pila A e Pila B a v0.2.0**: i file in `data/stage_3/few_shots/` e `data/stage_3/test/` sono stati annotati sotto le regole v0.1.0. Vanno rivisti a mano per allinearli alle nuove regole (deframmentazione delle scene, antefatti spostati in description, eventuale aggiunta di Theme incarnati). Bloccante per il prossimo run di estrazione: esempi incoerenti col prompt confondono il modello, soprattutto i modelli piccoli.

---

## Setup mentale (prima di leggere)

Fissa due cose nella testa prima ancora di guardare il testo:

1. **Cosa cerco.** Lo schema dice: Person, Event, Place, Phase, Theme, Reflection, Work. Tutto quello che non rientra in queste sette categorie non esiste per me in questo passaggio. La restrizione è disciplina, non povertà.

2. **A quale domanda risponderà il grafo.** Non sto facendo un riassunto. Sto costruendo materiale da cui un agente farà parlare Adriano in prima persona. Mi devo chiedere: *quando l'agente dovrà rispondere a una domanda sul contenuto di questo chunk, quali sono le domande possibili e quale grafo gliele permette?*

---

## Chunk-esempio di lavoro: meditazione su Grecia e Roma

> Tutte le volte che, alla svolta d'una strada assolata, ho levato lo sguardo da lunge su un'acropoli greca, sulla sua città, perfetta come un fiore, unita alla sua collina come il calice allo stelo, ho sentito che quella pianta incomparabile trovava un limite nella sua stessa perfezione, raggiunta in un dato punto dello spazio, in una definita frazione di tempo. Come quella delle piante, l'unica sua possibilità di espandersi consiste nel seme: quel germe di idee mediante le quali la Grecia ha fecondato il mondo. Ma Roma, più opulenta, più informe, adagiata senza contorni netti lungo il suo fiume, nella sua pianura, si disponeva verso sviluppi più vasti: la città è divenuta lo Stato. Avrei voluto che lo Stato si ampliasse ancora, divenisse ordine del mondo, ordine delle cose. Le virtù che erano sufficienti per la piccola città dai sette colli avrebbero dovuto farsi duttili, varie, per adeguarsi a tutta la terra. Roma, che io per primo osai qualificare eterna, si sarebbe assimilata sempre più alle dee madri dei culti dell'Asia: progenitrice di giovinetti e di messi, con leoni e alveari stretti al seno. Ma qualsiasi creazione umana che pretenda all'eternità è costretta a adattarsi al ritmo mutevole dei grandi eventi della natura, conformarsi al mutare degli astri.

---

## Passata 1 — Lettura lenta, senza penna in mano

**Obiettivo**: capire di cosa parla, in una riga. Non annotare nulla.

Sintesi: *Adriano confronta la Grecia (compiuta nella sua perfezione locale, espansa solo per via di idee) con Roma (informe ma capace di farsi Stato e ordine del mondo), e chiude con una sentenza sul limite di ogni creazione umana che voglia essere eterna.*

**Regola**: se dopo la prima lettura non sai dire di cosa parla, rileggi. Non annotare un testo che non hai capito.

---

## Passata 2 — Verbi situati nel tempo

**Cerco**: passato remoto, trapassato remoto, imperfetto narrativo. Sono i candidati Event.

Spie sul chunk-esempio:
- *"ho levato lo sguardo"* — passato prossimo in proposizione iterativa ("tutte le volte che..."). Event **abituale**, non singolo.
- *"ho sentito"* — sempre nell'iterazione, atto interiore abituale.
- *"si disponeva"* — imperfetto descrittivo. Stato, non Event.
- *"la città è divenuta lo Stato"* — processo storico ampio, ambiguo.
- *"Avrei voluto"* — condizionale passato del desiderio non realizzato. Event interiore retrospettivo.
- *"io per primo osai qualificare eterna"* — passato remoto, **Event puntuale e attribuibile**.

**Inventario Event**: tre, di cui due interiori e uno puntuale. Notare la povertà di Event: il chunk è meditativo per natura. Non è errore.

### Grana: scena vs fotogramma (regola v0.2.0)

Trovati i candidati, si decide la **grana**. Estrai Event al livello di **scena** (unità mnemonica narrativamente coesa), non di fotogramma (singoli gesti, sguardi, stati emotivi del momento).

Esempio canonico (passaggio dell'Eufrate):
> *"Adriano traversò l'Eufrate su una zattera. Flegone era pallido, gli ufficiali apprensivi, Opramoas a suo agio. Adriano era straordinariamente calmo. Restituì la principessa al padre."*

Lettura corretta (v0.2.0):
- **Una sola Event scena**: `incontro_diplomatico_eufrate`. La description ingloba la traversata, il pallore di Flegone, l'apprensione degli ufficiali, l'agio di Opramoas. NON sono nodi.
- **Un Event distinto come atto politico**: `restituzione_principessa`. Atto con conseguenze autonome, non dettaglio della scena.
- **Eccezione contrasto esplicito**: se il narratore mette esplicitamente in opposizione due stati ("la mia calma" vs "l'apprensione del seguito"), questi diventano Event distinti collegati da `CONTRASTS_WITH`. Soglia alta: il contrasto deve essere marcato dal testo, non desunto.

Più scene narrative riconoscibili dentro lo stesso chunk → più Event collegati da `ECHOES`.

### Antefatti in subordinata (regola v0.2.0)

Fatti del passato richiamati di sfuggita in una subordinata — es. *"il trono che Traiano aveva portato via"* — **NON** diventano Event autonomi. Vivono nella description del nodo a cui si riferiscono. Emergeranno come Event veri solo se un altro chunk li racconta per esteso. Demanda alla deduplicazione di stadio 4.

---

## Passata 3 — Nomi propri e luoghi

NER quasi puro.

**Person**: solo Adriano (implicito).

**Place**: acropoli greca (archetipo, va estratto), Grecia, Roma, Asia. "Sette colli" è metonimia per Roma, non Place a sé.

**Lezione**: quando un nome geografico funziona come archetipo (acropoli greca generica), va comunque estratto come Place, ma con descrizione che chiarisce il valore generico.

---

## Passata 4 — Reflection

Cerco frasi che esprimano valutazione, sentenza generale, commento — e che richiedano la prospettiva del vecchio narratore.

**Spie linguistiche**:
- presente gnomico (verbo al presente in proposizione generale)
- prima persona valutativa (comprendo, ammetto, oso)
- condizionali del rimpianto (avrei voluto, avrei dovuto)
- sentenze morali o filosofiche

**Inventario Reflection** nel chunk-esempio:
1. Tesi sul limite della perfezione greca.
2. Tesi sull'espansione greca tramite le idee.
3. Confessione del desiderio politico non realizzato (ampliamento dello Stato).
4. Tesi sulla necessità di virtù duttili per uno Stato universale.
5. Sentenza finale sull'eternità delle creazioni umane.

Cinque Reflection sono tante, ma il chunk è meditativo. In un chunk-scena ne attendi 0-2.

---

## Passata 5 — Theme

Regole base (v0.1.0, ancora valide):
- estrai solo i Theme che il chunk *insiste*, non quelli generici del libro
- estrai solo Theme con una manifestazione concreta nel chunk
- pochi Theme per chunk; se ne stai trovando cinque, stai sovra-estraendo

Aggiornamento v0.2.0 — **Theme incarnato**:
- Estrai un Theme **sia quando è nominato esplicitamente, sia quando il paragrafo lo INCARNA attraverso le proprie scene e atti**, anche senza nominarlo.
- **Test pratico**: se gli Event del paragrafo sembrano tutti orientati a illustrare una stessa idea astratta, quella idea è un Theme.
- Esempio: una scena di esecuzione esemplare descritta con tutti i suoi atti politici incarna il Theme "esercizio della giustizia imperiale" anche se la parola "giustizia" non appare. Estrai.
- Limite (per evitare invenzione): l'incarnazione deve essere visibile nel chunk. Se per giustificarla devi appellarti a quello che sai del resto del libro, non estrarre.

**Theme estratti** nel chunk-esempio: "modello greco e modello romano", "eternità delle creazioni umane", "universalismo dello Stato". Tre, al limite alto.

---

## Passata 6 — Archi sistematici

**Per ogni Event**:
- Chi è coinvolto? → INVOLVES
- Dove? → LOCATED_AT
- In quale Phase? → DURING
- Cosa l'ha causato? → CAUSED
- Cosa segue temporalmente? → FOLLOWS
- Cosa incarna tematicamente? → EMBODIES
- Riprende / fa eco a un altro Event (anche dentro lo stesso chunk se sono scene distinte del medesimo episodio composto)? → ECHOES
- È in opposizione esplicita con un altro Event (contrasto marcato dal testo, non desunto)? → CONTRASTS_WITH

**Per ogni Reflection**:
- Su cosa riflette? → REFLECTS_ON (verso Event, Theme, o altri nodi)

### Uso di `CONTRASTS_WITH` (v0.2.0)

Cautela: `CONTRASTS_WITH` va usato **solo** quando il testo marca esplicitamente l'opposizione. Caso tipico: due stati o due comportamenti che il narratore mette uno accanto all'altro nella stessa frase ("la mia calma vs la loro apprensione"). Non usarlo per contrasti che TU vedi ma che il testo non enuncia.

### Uso di `ECHOES` (v0.2.0)

Se un episodio "composto" si articola su scene distinte ma collegate nello stesso chunk (es. "andai al tempio, poi al porto, poi tornai") e ognuna ha sostanza propria, estrai gli Event separati e collegali con `ECHOES`. Se invece è un'unica scena con dettagli interni, NON spezzarla: vivono nella description (vedi Passata 2 sulla grana).

**Lezione su chunk meditativi**: il grafo si regge su molti archi REFLECTS_ON che collegano Reflection a pochi Theme centrali. È normale. È il modo in cui il grafo cattura "il pensiero del narratore" invece di una scena.

---

## Passata 6.5 — Test di densità (v0.2.0)

Una check rapida prima di chiudere. Un paragrafo denso di prosa di Yourcenar produce **tipicamente** una manciata di nodi:

- una scena cardine (Event)
- uno o due atti politici o interiori distinti (Event)
- gli attori della scena (Person)
- il luogo (Place)
- uno o due temi (Theme)
- una o due riflessioni del narratore (Reflection)

Indicativo, non obbligatorio. Se il paragrafo è povero, estrai poco. Se è meditativo, molte Reflection e pochi Event. Se ti accorgi di avere venti Event per un paragrafo di scena unica, probabilmente hai frammentato (rileggi la regola sulla grana).

---

## Passata 7 — Verifica finale

Tre controlli:

**(1) Provenance per tutto.** Ogni nodo e arco ha un `evidence_span` letterale dal chunk. Se per qualcosa non riesco a indicare la sottostringa che lo giustifica, l'estrazione è inventata. Rimuovi.

**(2) Coerenza interna.** Ogni `source_id` e `target_id` esiste tra i nodi. Niente archi orfani.

**(3) Test di restituzione.** *L'agente conversazionale, con questo grafo, saprebbe rispondere alle domande che il chunk permette?* Se una domanda plausibile resta scoperta, aggiungi. Se un nodo non aiuta a rispondere a nessuna domanda plausibile, valuta se è davvero necessario.

---

## Sette passate, riepilogo

1. Lettura senza penna — capire di cosa parla.
2. Verbi situati — candidati Event, con grana a scena (non fotogramma) e antefatti che restano in description.
3. Nomi propri e luoghi — Person e Place (NER).
4. Frasi gnomiche e valutative — candidati Reflection.
5. Temi insistiti o incarnati — Theme, con parsimonia ma includendo i temi non lessicalizzati che gli Event illustrano.
6. Archi sistematici — per ogni nodo, le sue connessioni. CONTRASTS_WITH solo se esplicito, ECHOES per scene multiple dentro un episodio composto.
6.5. Test di densità — confronto con il profilo tipico, indicatore di frammentazione o sotto-estrazione.
7. Verifica — provenance, coerenza, test di restituzione.

## Tre principi trasversali

- **Sotto-estrai prima di sovra-estrarre.** Meglio un grafo sparso e fedele di uno denso e inventato.
- **La provenance è non-negoziabile.** Se non trovo `evidence_span` letterale dal chunk, l'estrazione non esiste.
- **L'esitazione è un dato.** I punti dove esito vanno annotati (confidence bassa) e discussi, non risolti a forza.

## Sospetti di errore ricorrenti (checklist prima di chiudere)

- Ho lasciato archi a zero? → torna alla Passata 6.
- Ho usato parafrasi negli `evidence_span`? → sostituisci con citazione letterale.
- Ho estratto un Theme da una singola occorrenza descrittiva? → probabilmente toglilo. (Eccezione v0.2.0: se più Event lo incarnano congiuntamente, anche senza nominarlo, può restare.)
- Ho confuso Adriano-personaggio con Adriano-narratore? → ricontrolla i tempi verbali e i condizionali.
- Ho `chunk_id` incoerenti tra nodi e archi? → uniformali.
- JSON valido? Niente virgole dopo l'ultimo elemento di un array? Tutte le stringhe tra virgolette? → valida con un linter.
- **(v0.2.0)** Ho frammentato una scena unica in più Event, uno per gesto o stato emotivo? → consolida in un Event scena con i dettagli nella description.
- **(v0.2.0)** Ho promosso a Event un antefatto evocato in subordinata, che il chunk non racconta per esteso? → spostalo nella description del nodo a cui si riferisce.
- **(v0.2.0)** Ho usato `CONTRASTS_WITH` per un'opposizione che vedo io ma il testo non marca? → rimuovi.
- **(v0.2.0)** Confronto col profilo di densità tipica: il mio conteggio è coerente con un paragrafo della stessa natura? Se ne ho il triplo, ho frammentato. Se quasi nulla, ho sotto-estratto.

## Decisioni borderline tipiche del progetto Yourcenar

- **Pensieri di Adriano-personaggio nel momento**: Event interiore, non Reflection.
- **Sentenze gnomiche al presente generale**: Reflection.
- **Condizionali passati ("avrei voluto", "avrei dovuto")**: spia forte di Reflection retrospettiva.
- **Metafore estese (abeti, acropoli come pianta)**: tipicamente Reflection sotto forma di immagine, non Event.
- **Luoghi archetipici (acropoli greca generica)**: Place con descrizione del valore generico.
- **Persone citate senza relazioni nel chunk**: nodi isolati legittimi. Un nodo isolato è informazione, non bug.
- **Chunk meditativi (digressioni filosofiche, politiche, estetiche)**: pochi Event, molte Reflection, molti REFLECTS_ON verso pochi Theme centrali. Non forzare equilibrio tra registri.
- **(v0.2.0) Scena con dettagli interni (gesti, stati emotivi, schieramenti del seguito)**: una sola Event scena, dettagli nella description. NON un Event per gesto.
- **(v0.2.0) Atto politico distinto dentro una scena (decisione con conseguenze autonome)**: Event a sé, non assorbito nella scena.
- **(v0.2.0) Antefatti in subordinata (es. "il trono che X aveva portato via")**: NON Event autonomi. Vivono nella description.
- **(v0.2.0) Contrasto esplicito marcato dal testo (es. "io ero calmo, loro apprensivi")**: Event distinti collegati da `CONTRASTS_WITH`. Senza marca esplicita, niente arco.
- **(v0.2.0) Tema incarnato ma non nominato (gli Event sono tutti orientati a illustrare la stessa idea)**: estraibile come Theme. Limite: l'incarnazione deve essere visibile nel chunk, non desunta da altri.
