# Guida passo-passo all'annotazione di un chunk

> Documento di lavoro. Aggiornare quando il metodo si raffina.
> Ultimo aggiornamento: stato delle Pile + cautele post-selezione.

---

## Stato attuale del prototipo Stadio 3

- **Pila A (esempi few-shot)**: 4 chunk selezionati e annotati a mano.
- **Pila B (test gold-standard)**: 4 chunk selezionati e annotati a mano, non visti dal modello.
- **Pila C (casebook di casi-limite)**: in costruzione. Vi confluiscono i chunk troppo ambigui per essere esempi e i casi su cui le due chat di confronto hanno divergato.

**Cautele aperte** (da affrontare nella chat di implementazione):

1. **Token budget**: 4 esempi few-shot + schema + istruzioni rischiano di superare i 6-7k token di prompt. Se accade, mollare l'esempio più debole e tenerne 3. Gli esempi marginali costano più di quanto rendano.
2. **Test sotto-dimensionati**: 4 chunk di test sono sufficienti per validare qualitativamente il prompt nel prototipo, non per la valutazione finale. Debito tecnico: estendere a 10-15 chunk di test, distribuiti su tutte e sei le parti del libro, prima del run completo.
3. **Divergenze tra le due chat di annotazione**: ogni divergenza è un caso da mettere nel casebook. Significa che il contratto ha ancora ambiguità, e quelle ambiguità riemergeranno con l'estrattore.

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

Tre regole:
- estrai solo i Theme che il chunk *insiste*, non quelli generici del libro
- estrai solo Theme con una manifestazione concreta nel chunk
- pochi Theme per chunk; se ne stai trovando cinque, stai sovra-estraendo

**Theme estratti** nel chunk-esempio: "modello greco e modello romano", "eternità delle creazioni umane", "universalismo dello Stato". Tre, al limite alto.

---

## Passata 6 — Archi sistematici

**Per ogni Event**:
- Chi è coinvolto? → INVOLVES
- Dove? → LOCATED_AT
- In quale Phase? → DURING
- Cosa l'ha causato? → CAUSED
- Cosa incarna tematicamente? → EMBODIES

**Per ogni Reflection**:
- Su cosa riflette? → REFLECTS_ON (verso Event, Theme, o altri nodi)

**Lezione su chunk meditativi**: il grafo si regge su molti archi REFLECTS_ON che collegano Reflection a pochi Theme centrali. È normale. È il modo in cui il grafo cattura "il pensiero del narratore" invece di una scena.

---

## Passata 7 — Verifica finale

Tre controlli:

**(1) Provenance per tutto.** Ogni nodo e arco ha un `evidence_span` letterale dal chunk. Se per qualcosa non riesco a indicare la sottostringa che lo giustifica, l'estrazione è inventata. Rimuovi.

**(2) Coerenza interna.** Ogni `source_id` e `target_id` esiste tra i nodi. Niente archi orfani.

**(3) Test di restituzione.** *L'agente conversazionale, con questo grafo, saprebbe rispondere alle domande che il chunk permette?* Se una domanda plausibile resta scoperta, aggiungi. Se un nodo non aiuta a rispondere a nessuna domanda plausibile, valuta se è davvero necessario.

---

## Sette passate, riepilogo

1. Lettura senza penna — capire di cosa parla.
2. Verbi situati — candidati Event.
3. Nomi propri e luoghi — Person e Place (NER).
4. Frasi gnomiche e valutative — candidati Reflection.
5. Temi insistiti — Theme, con parsimonia.
6. Archi sistematici — per ogni nodo, le sue connessioni.
7. Verifica — provenance, coerenza, test di restituzione.

## Tre principi trasversali

- **Sotto-estrai prima di sovra-estrarre.** Meglio un grafo sparso e fedele di uno denso e inventato.
- **La provenance è non-negoziabile.** Se non trovo `evidence_span` letterale dal chunk, l'estrazione non esiste.
- **L'esitazione è un dato.** I punti dove esito vanno annotati (confidence bassa) e discussi, non risolti a forza.

## Sospetti di errore ricorrenti (checklist prima di chiudere)

- Ho lasciato archi a zero? → torna alla Passata 6.
- Ho usato parafrasi negli `evidence_span`? → sostituisci con citazione letterale.
- Ho estratto un Theme da una singola occorrenza descrittiva? → probabilmente toglilo.
- Ho confuso Adriano-personaggio con Adriano-narratore? → ricontrolla i tempi verbali e i condizionali.
- Ho `chunk_id` incoerenti tra nodi e archi? → uniformali.
- JSON valido? Niente virgole dopo l'ultimo elemento di un array? Tutte le stringhe tra virgolette? → valida con un linter.

## Decisioni borderline tipiche del progetto Yourcenar

- **Pensieri di Adriano-personaggio nel momento**: Event interiore, non Reflection.
- **Sentenze gnomiche al presente generale**: Reflection.
- **Condizionali passati ("avrei voluto", "avrei dovuto")**: spia forte di Reflection retrospettiva.
- **Metafore estese (abeti, acropoli come pianta)**: tipicamente Reflection sotto forma di immagine, non Event.
- **Luoghi archetipici (acropoli greca generica)**: Place con descrizione del valore generico.
- **Persone citate senza relazioni nel chunk**: nodi isolati legittimi. Un nodo isolato è informazione, non bug.
- **Chunk meditativi (digressioni filosofiche, politiche, estetiche)**: pochi Event, molte Reflection, molti REFLECTS_ON verso pochi Theme centrali. Non forzare equilibrio tra registri.
