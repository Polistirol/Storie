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

from src.schema import SCHEMA_VERSION, INVOLVES_ROLES  # noqa: F401  (SCHEMA_VERSION esportato per provenance)

PROMPT_VERSION = "0.4.2"

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
    "ch_0092.json",   # scena narrativa con cast e ruoli INVOLVES
    "ch_0199.json",   # scena con stati interni + contrasto esplicito
    "ch_0016.json",   # riflessione meditativa pura
    "ch_0103.json",   # evento istituzionale + Theme + Phase
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
(entità) e archi (relazioni) da un singolo paragrafo di un testo
biografico in prima persona.

Il narratore è anche il protagonista della biografia: ricorda la propria
vita e la commenta. Il testo alterna scene del passato e riflessioni del
presente narrativo. Lo stile è meditativo: il narratore non si limita
a raccontare i fatti, li interpreta.

# Profilo del soggetto della biografia

<!-- NEW 0.4.0 -->
Il soggetto di questa biografia è descritto nel blocco
<subject_profile> qui sotto. Contiene nome canonico, soprannomi,
anni di vita, ruolo, e altre informazioni biografiche note a priori.
USA queste informazioni per:
- riconoscere il soggetto anche quando il testo lo nomina con varianti
  o pronomi (è quasi sempre il "io" del testo);
- ancorare correttamente gli Event alla fase di vita giusta (Era,
  vedi sotto);
- riconoscere le Person significative del suo entourage senza inventarle
  ex novo.

NON estrarre campi del profilo che non compaiono nel chunk corrente:
il profilo è contesto, non contenuto da duplicare.

<subject_profile>
{SUBJECT_PROFILE}
</subject_profile>

# Tipi di nodo

- Person: persona nominata o chiaramente identificabile.
  Usa la forma italiana canonica. Il soggetto della biografia (vedi
  profilo sopra) è una Person come le altre, ma è SEMPRE protagonista
  degli Event in cui agisce (vedi ruoli su INVOLVES).

  <!-- MODIFIED 0.4.0 / EXAMPLES EXTENDED 0.4.1 -->
  ISTITUZIONI come attori collettivi (Senato, Curia, Pretorianato,
  città-stato, popoli, etnie o imperi che agiscono o decidono — "il
  Senato decretò", "Atene si oppose", "i Parti restavano fedeli ai
  trattati", "Israele non poteva dirci di sì") sono Person, anche
  quando il nome indica uno stato, un popolo o un'etnia. La loro sede
  fisica, se rilevante, è un Place separato collegato via LOCATED_AT.
  Mai Place direttamente.

- Event: una SCENA della vita del soggetto, intesa come unità mnemonica
  narrativamente coesa: un episodio, un atto, un momento riconoscibile.
  Battaglie, viaggi, incontri, morti, decisioni, riti, atti politici,
  cerimonie. Sempre qualcosa che ACCADE in un momento situabile, anche
  vagamente.

  GRANA: estrai a livello di scena, non di fotogramma. Una scena ingloba
  i suoi dettagli interni (gesti, sguardi, schieramenti, stati fisici o
  emotivi del momento) nella propria description, non in nodi separati.

  Esempio: "Adriano traversò l'Eufrate su una zattera. Flegone era pallido,
  gli ufficiali apprensivi, Opramoas a suo agio. Adriano era straordinariamente
  calmo. Restituì la principessa al padre."

  -> Event scena: "incontro_diplomatico_eufrate", la cui description
     include la traversata, il pallore di Flegone, l'apprensione degli
     ufficiali, l'agio di Opramoas.
  -> Event atto politico distinto: "restituzione_principessa" (atto
     autonomo con conseguenze, non dettaglio della scena).
  -> Event distinti per contrasto esplicito: "calma_di_adriano" e
     "apprensione_seguito", collegati da CONTRASTS_WITH, perché il testo
     li mette uno accanto all'altro esplicitamente.

  Nota: se un Event è composto da più scene, estraili come Event distinti
  collegati da ECHOES. ECCEZIONE: se il testo CONTRASTA ESPLICITAMENTE
  due stati o due comportamenti interni alla scena (es. la mia calma vs
  la loro apprensione), estraili come Event distinti collegati da
  CONTRASTS_WITH. Soglia alta: contrasto marcato dal testo, non desumibile.

  <!-- STRENGTHENED 0.4.1 -->
  ANTEFATTI in subordinata o in enumerazione: fatti del passato
  richiamati di sfuggita in una subordinata (es. "il trono che Traiano
  aveva portato via") NON diventano Event autonomi. Vivono nella
  description del nodo cui si riferiscono.

  La regola vale ANCHE — e SOPRATTUTTO — quando il chunk enumera più
  antefatti in sequenza paratattica. Esempio canonico: "Marullino era
  morto. Mio padre era morto. Mia madre era morta. Traiano non era
  stato che un infermo. Plotina non l'avevo vista morire. Attiano era
  morto." Le SEI persone enumerate NON vanno estratte come sei Person
  isolate, né le sei morti come sei Event: tutte le persone e le loro
  morti vivono nella description di UN'UNICA Reflection di enumerazione
  retrospettiva (modalità 2). Lo stesso vale per i luoghi richiamati
  di sfuggita in funzione di antefatto (Danubio, Colosseo, Pannonia
  nello stesso esempio): non Place autonomi, ma dettagli nella
  description della Reflection.

  Segnali linguistici dell'enumerazione di antefatti:
  - ritmo paratattico ripetitivo ("X era morto. Y era morta. Z era
    morto");
  - trapassati prossimi applicati a soggetti diversi in sequenza
    ("avevo visto", "avevo perduto", "mi avevano mostrato");
  - funzione narrativa di SFONDO al momento presente, non di scena
    propria.

  Person, Event e Place così evocati emergeranno individualmente se e
  quando un altro chunk li racconta per esteso.

  EVENT INTERNO (stato d'animo rivissuto): un'eccezione alla regola
  scena. Quando il narratore non commenta dall'esterno ma rivive uno
  stato interiore di sé-allora come accadimento situato, quello stato è
  un Event interno, non una Reflection. Esempio: "Una calma straordinaria
  era scesa su di me" non è giudizio del narratore vecchio, è il
  riaffiorare di un cambio di stato avvenuto in un momento preciso.

  <!-- STRENGTHENED 0.4.1 -->
  Tre criteri obbligatori, valutati come CHECKLIST HARD GATE. Per
  estrarre un Event interno DEVI poter rispondere SÌ a tutti e tre in
  modo indipendente e verificabile sul testo:

  [ ] (i)   cambio di stato esplicito: qualcosa che PRIMA non c'era e
            ORA c'è (o viceversa), formulato come passaggio puntuale e
            non come disposizione durativa.
  [ ] (ii)  ancoraggio temporale puntuale, esplicito o ricavabile: "quel
            giorno", "in quel momento", "quando", "allora", "dopo che",
            "appena". Una scena di sfondo durativa NON soddisfa il
            criterio: serve un PUNTO nel tempo.
  [ ] (iii) marca verbale puntuale: passato remoto ("scese", "mi accorsi",
            "sentii", "fui colto", "raccolsi le mie idee"), passato
            prossimo puntuale ("è scesa"), piuccheperfetto puntuale
            ("era scesa"). NON ammessi come (iii):
              - imperfetti durativi ("mi rimproveravo", "ci sentivamo",
                "ero sempre", "mi pareva", "non mi preoccupava più");
              - trapassati durativi ("ero stato sempre deciso");
              - presenti gnomici o valutativi ("oggi comprendo", "mi
                rendo conto");
              - condizionali del rimpianto ("avrei dovuto", "avrei
                voluto");
              - esortativi al presente ("cerchiamo di X", "guardiamo
                insieme", "entriamo").

  Se anche UNO solo dei tre criteri è dubbio o assente, l'Event interno
  NON va estratto. In caso di dubbio: l'enunciato vive nella description
  di un Event esterno pertinente, oppure diventa Reflection (modalità 1
  o 2).

  Anti-pattern frequenti che SEMBRANO Event interno ma NON lo sono
  (criterio (iii) mancante):
  - "mi rimproveravo d'esser stato cieco": imperfetto durativo
    → Reflection (2).
  - "ci sentivamo riportati in quel mondo eroico": imperfetto durativo
    collettivo → resta nella description della scena cornice.
  - "Cerchiamo d'entrare nella morte a occhi aperti": voce esortativa
    al presente, nessun cambio puntuale → Reflection-invocazione, mai
    Event.
  - "Ero stato sempre deciso a difendere le mie probabilità": trapassato
    durativo, disposizione → Reflection (2).

  AGGANCIO OBBLIGATORIO. Un Event interno DEVE avere almeno un arco
  verso un Event esterno o una Phase/Era nel chunk stesso. Scegli in
  quest'ordine:
    1) CAUSED se il testo marca il nesso (anche solo con "dopo che",
       "perché", "ormai", "grazie a", o per evidente implicazione
       narrativa). Direzione canonica: Event esterno -> Event interno.
    2) FOLLOWS se non c'è marca causale ma c'è successione temporale
       chiara con un Event esterno del chunk.
    3) DURING verso una Phase o Era, se manca un Event esterno-ancora
       nello stesso chunk ma la fase di vita è esplicita.
  Se non riesci ad agganciare l'Event interno con almeno uno di questi
  archi, NON estrarlo: vivrà nella description dell'Event esterno
  pertinente.

  <!-- NEW 0.4.0 -->
  EVENTI STORICI NOMINALI: ogni evento puntuale della biografia che ha
  un nome canonico ("Morte di Antinoo", "Matrimonio con Sabina",
  "Adozione di Lucio", "Battaglia di X") è SEMPRE Event, anche quando
  il chunk corrente lo richiama come tema ricorrente o come momento
  spartiacque di una fase di vita. La sua eventuale dimensione tematica
  va estratta come Theme SEPARATO (es. `lutto_per_antinoo` Theme accanto
  a `morte_di_antinoo` Event), con un EMBODIES dall'Event al Theme. Mai
  Phase, mai Theme al posto di Event.

  <!-- NEW 0.4.0 -->
  FONDAZIONI EDILIZIE: se il chunk parla DELL'ATTO di fondare o
  costruire (Antinopoli, Aelia Capitolina, Villa Adriana, il Pantheon
  ricostruito), è Event. Se parla DELL'OGGETTO come luogo (essere a
  Villa Adriana, abitare ad Antinopoli), è Place. Mai Work. Work è
  riservato a opere mobili: libri, ritratti, statue, manoscritti, leggi
  codificate, monete.

- Place: luogo geografico nominato o chiaramente implicato.
  Forma italiana canonica. Anche un luogo archetipico (acropoli greca
  generica) va estratto come Place, con descrizione che chiarisce il
  valore generico.

- Era (NUOVO in 0.4.0): la fase universale della vita del soggetto in cui
  ricade un Event. Set CHIUSO di quattro valori, da usare ESATTAMENTE
  con questi id:
    `infanzia`     -> da 0 a ~14 anni
    `gioventu`     -> da ~14 a ~30 anni
    `adultita`     -> da ~30 a ~60 anni
    `vecchiaia`    -> oltre ~60 anni
  Le soglie sono indicative: usa il profilo del soggetto (anni di vita)
  per stimare l'Era. Se il chunk non offre alcun ancoraggio temporale,
  ometti l'aggancio Era (vedi regole operative).

  L'Era è la SPINA DORSALE TEMPORALE del grafo. Ogni Event va ancorato
  a una Era via DURING quando il testo lo permette.

- Phase: un periodo specifico e nominabile della vita del soggetto
  (matrimonio con X, anni di guerra, malattia terminale, viaggio
  pluriennale, periodo come funzionario in città Y) o un'era storica
  delimitata che il testo nomina (regno di Traiano). Le Phase sono il
  DETTAGLIO temporale specifico, complementare alle Era.

  Estrai una Phase SOLO se il testo la delimita in modo riconoscibile
  (inizio o fine identificabili, anche solo per indizio). Quando estrai
  una Phase, agganciala alla Era corrispondente via OCCURS_IN.

- Theme: un tema astratto su cui il paragrafo insiste o che mette in
  scena (la morte, il potere, l'amore, la memoria, il corpo, la fiducia,
  l'oblio). Estrai un Theme sia quando è nominato esplicitamente, sia
  quando il paragrafo lo INCARNA attraverso le proprie scene e atti,
  anche senza nominarlo.

  Test pratico: se gli Event del paragrafo sembrano tutti orientati a
  illustrare una stessa idea astratta, quella idea è un Theme.

  <!-- NEW 0.4.0 -->
  CANONICITÀ DEL NAME. Il `name` di un Theme è un sintagma BREVE e
  GENERALE (idealmente ≤3 parole sostantive), pensato per essere riusato
  attraverso il libro: "la morte", "il lutto", "memoria e immortalità",
  "decoro pubblico", "successione". La specificità del Theme nel chunk
  corrente va in `description`, non in `name`. Se hai in mente due
  possibili name che differiscono solo per dettagli ("bellezza",
  "bellezza e indifferenza a se stessa", "bellezza e sublime"), scegli
  il più breve e generale. L'id del Theme segue la stessa regola:
  `bellezza`, non `bellezza_e_indifferenza`.

- Reflection: una considerazione, valutazione o sentenza del NARRATORE
  (il soggetto della biografia che scrive, oggi) SUI fatti. Vedi
  distinzione critica sotto.

- Work: un'opera mobile attribuibile a qualcuno: libri, scritti, statue,
  ritratti, leggi codificate, monete, manoscritti. NON edifici, NON
  fondazioni urbanistiche (quelli sono Event o Place).

# Distinzione critica: Event vs Reflection

Il testo è scritto in forma di memoria. Alterna continuamente:

  (a) il racconto di un fatto              -> Event
  (b) il commento del narratore sul fatto  -> Reflection

Esempio:
  "Andai a caccia in Bitinia con Antinoo. Comprendo solo oggi che quei
   mesi furono il vertice della mia felicità."

  -> Event:      "caccia in Bitinia con Antinoo"
  -> Reflection: "comprensione retrospettiva che quei mesi furono il
                  vertice della felicità"
  -> Edge:       la Reflection REFLECTS_ON l'Event.

Tre modalità riflessive da distinguere, perché solo le prime due sono
Reflection; la terza è Event interno (vedi sopra).

 (1) Riflessione gnomica del narratore-oggi. Presente generale, sentenza
     valida fuori dal tempo. Es. "qualsiasi creazione umana che pretenda
     all'eternità è costretta a adattarsi al ritmo della natura".
     -> Reflection.

 (2) Giudizio retrospettivo del narratore-oggi su sé-allora. Disposizione
     durativa che il narratore attribuisce a sé giovane. Es. "ero stato
     sempre deciso a difendere le mie probabilità di diventare imperatore".
     Verbi imperfetti, trapassati durativi, condizionali del rimpianto.
     -> Reflection.

 (3) Stato interiore di sé-allora rivissuto. Il vecchio non giudica
     dall'esterno ma rientra dentro sé-allora e ne riporta il cambio di
     stato come accaduto in un momento. Es. "una calma straordinaria
     era scesa su di me". Tre criteri (cambio + ancoraggio + verbo
     puntuale) tutti presenti.
     -> Event interno, NON Reflection.

Segnali linguistici di Reflection:
- tempi al presente o passato prossimo in mezzo a un passato remoto
- prima persona valutativa: "comprendo", "ora so", "mi rendo conto",
  "ammetto", "oso"
- sentenze gnomiche, massime generali, presente gnomico
- giudizi morali, estetici, filosofici espressi dal narratore
- condizionali del rimpianto: "avrei voluto", "avrei dovuto"

In caso di dubbio fra (2) e (3), scegli (2) Reflection: è la modalità
dominante, ed estrarre un Event interno richiede che TUTTI E TRE i
criteri siano evidenti.

In caso di dubbio: estrai entrambi e marca la Reflection con confidence
bassa.

# Tipi di arco

Fattuali:
- INVOLVES        Event -> Person, con CAMPO `role` obbligatorio
- LOCATED_AT      Event -> Place
- DURING          Event -> Era oppure Event -> Phase  (ancoraggio temporale)
- OCCURS_IN       Phase -> Era  (NUOVO in 0.4.0)
- CREATED         Person -> Work
- RELATED_TO      jolly generico. USARE SOLO come ultima spiaggia, quando
                  nessun altro arco dello schema si adatta. Non usare per
                  connettere Theme affini o Person correlate: la relazione
                  semantica resta nelle description, non in un arco.

Riflessivi / tematici:
- EMBODIES        Event o Person -> Theme    (il fatto incarna il tema)
- REFLECTS_ON     Reflection -> qualsiasi nodo
- ECHOES          Event -> Event             (eco narrativo, ripresa)
- TRANSFORMS_INTO Phase->Phase, Person->Person  (cambiamento interiore)
- CONTRASTS_WITH  Event<->Event, Theme<->Theme.
  Da usare SOLO quando il testo marca esplicitamente l'opposizione con
  connettori avversativi ("ma", "invece", "al contrario", "mentre"), con
  giustapposizione antitetica marcata, o con formule esplicite di
  contrasto ("X non era Y", "io X, lui Y"). NON usare per contrasti
  desunti dall'estrattore, per temi articolati su gradi diversi della
  stessa cosa, per temi complementari, o per tensioni interpretative.

  <!-- NEW 0.4.2 -->
  Anti-pattern frequenti che SEMBRANO contrasto ma NON lo sono:
  - temi articolati come gradi di intensità della stessa cosa ("contatti
    superficiali" e "voluttà come grado supremo del contatto": NON
    contrasto, sono lo stesso continuum);
  - temi complementari che il chunk articola insieme ("lutto" e
    "inadeguatezza del linguaggio della morte": NON contrasto, il
    secondo è la conseguenza riflessiva del primo);
  - tensione interpretativa fra colpa soggettiva e limite oggettivo
    ("autoaccusa" e "limite del potere": NON contrasto, il chunk li
    tiene insieme nella stessa diagnosi);
  - simmetria poetica fra vita e morte in un'apostrofe ("morte" e
    "commiato dalla vita": NON contrasto, sono lati della stessa
    soglia).

  Soglia operativa: se non sai indicare la sottostringa col connettore
  avversativo esplicito o la formula antitetica nell'evidence_span,
  l'arco CONTRASTS_WITH non esiste. Omettere è sempre preferibile.

Causali / temporali:
- CAUSED          Event -> Event
- FOLLOWS         Event -> Event (successione temporale)

<!-- NEW 0.4.0 -->
## Ruoli su INVOLVES

Ogni arco INVOLVES deve avere un campo `role` con uno dei tre valori:

- `protagonist`: la persona AGISCE nella scena, è uno degli attori
  principali. Il soggetto della biografia (vedi profilo) è SEMPRE
  protagonist degli Event in cui compare attivamente.
- `participant`: la persona è presente e prende parte alla scena ma
  non come attore principale ("il mio seguito", "gli ufficiali",
  "i partecipanti al banchetto").
- `mentioned`: la persona è citata nell'Event ma non vi partecipa
  fisicamente (un assente di cui si parla, un nome storico evocato,
  "come ai tempi di X").

Test pratico:
- se rimuovo questa Person dalla scena, la scena ancora avviene? NO -> protagonist
- se rimuovo questa Person dalla scena, la scena è impoverita ma avviene? -> participant
- se rimuovo questa Person, la scena è invariata? -> mentioned

<!-- NEW 0.4.0 -->
## Priorità CAUSED sopra FOLLOWS

Tra due Event in successione temporale, PREFERISCI CAUSED a FOLLOWS
ogni volta che il testo suggerisce un nesso narrativo, anche solo
implicito:
- connettori espliciti: "dopo che", "perché", "ormai", "il che",
  "per questo", "grazie a", "a causa di", "in conseguenza";
- conseguenza politica o militare ("dopo la vittoria, ottenne il
  consolato");
- conseguenza emotiva o di carattere ("dopo il lutto, smise di
  viaggiare");
- evidente implicazione narrativa: il testo non lo dice ma il legame
  è ovvio dalla giustapposizione e dal contesto.

RISERVA FOLLOWS a successioni puramente cronologiche, senza nesso
narrativo: enumerazioni di date, consolati, tappe di un viaggio,
sequenze di anni inerti.

In dubbio fra i due, scegli CAUSED. Il testo è narrazione, non cronaca.

<!-- NEW 0.4.1 -->
## Errori comuni di compatibilità archi

Lo schema enforza vincoli sui tipi sorgente/destinazione di ogni arco.
Le violazioni più frequenti osservate in iterazioni precedenti, da
evitare attivamente:

- EMBODIES: solo Event o Person possono incarnare un Theme. Mai Phase,
  Reflection, Place, Work o Era come sorgente.
- TRANSFORMS_INTO: solo fra Phase, Person o Era della stessa famiglia.
  Mai fra Theme: temi diversi NON si trasformano l'uno nell'altro; se
  il testo li mette in opposizione marcata, l'arco corretto è
  CONTRASTS_WITH; altrimenti restano nodi distinti senza arco diretto.
- CONTRASTS_WITH: solo Event ↔ Event o Theme ↔ Theme. Mai Event ↔ Theme.
- ECHOES, CAUSED: solo Event → Event. Non usare per connettere
  Reflection o Theme fra loro o con Event.
- FOLLOWS: Event → Event oppure Era → Era. Non altro.
- INVOLVES: richiede sempre il campo `role`; ogni altro tipo di arco
  NON ammette `role`.

Nel dubbio, omettere l'arco è preferibile a forzarne uno non compatibile.

# Regole operative

1. Estrai SOLO ciò che il paragrafo afferma o implica fortemente, e non
   aggiungere conoscenza storica esterna al testo. Il profilo del
   soggetto serve a riconoscere, non a riempire.

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
   - role: OBBLIGATORIO se type = INVOLVES, OMESSO altrimenti
   - description: opzionale, perché esiste questo arco
   - confidence: 0.0-1.0
   - evidence_span: la sottostringa che giustifica la relazione

4. ANCORAGGIO TEMPORALE: ogni Event va ancorato a una Era via DURING
   quando il testo offre indizi sufficienti (riferimenti a età del
   soggetto, fase di carriera, eventi storici datati, posizione nel
   profilo). Se il chunk è una meditazione puramente atemporale e
   nessun ancoraggio è ricavabile, ometti il DURING. Le Phase
   emergenti, quando estratte, vanno ancorate a una Era via OCCURS_IN.

5. Se il paragrafo è puramente descrittivo e non offre entità nuove
   (es. una digressione astratta senza riferimenti concreti) restituisci
   nodi e archi vuoti. NON forzare estrazioni.

6. Sotto-estrai prima di sovra-estrarre. Meglio un grafo sparso e fedele
   di uno denso e inventato. La provenance (evidence_span letterale) è
   non negoziabile: se non riesci a indicare la sottostringa che la
   giustifica, l'estrazione non esiste.

7. Densità. Un paragrafo di prosa densa produce tipicamente una manciata
   di nodi: una scena cardine (Event), uno o due atti politici o interiori
   distinti (Event), gli attori della scena (Person), il luogo (Place),
   una Era di ancoraggio, talvolta una Phase, uno o due temi (Theme),
   da una a tre o quattro riflessioni del narratore (Reflection).
   Indicativo, non obbligatorio.

   Calibrazione sugli Event interni: di norma 0, occasionalmente 1,
   raramente 2 per chunk. Se ne stai estraendo più di uno, rileggi:
   probabilmente uno è (2) giudizio retrospettivo durativo travestito
   da (3) stato puntuale rivissuto.

   <!-- NEW 0.4.1, STRENGTHENED 0.4.2 -->
   Calibrazione sulle Reflection: quando il narratore-oggi alterna in
   uno stesso paragrafo sentenze gnomiche (modalità 1), giudizi
   retrospettivi su sé-allora (modalità 2), ritratti retrospettivi di
   altre persone e autoaccuse esplicite, ognuna di queste modalità
   distinte è una Reflection a sé, non un sotto-aspetto di un'unica
   macro-Reflection. La grana di voce del narratore è essenziale
   all'obiettivo biografico del grafo: NON accorpare in un singolo nodo
   riflessioni che il testo articola separatamente per soggetto, tempo
   verbale o funzione.

   Test pratico in DUE direzioni:
   - SPACCHETTARE quando: fra due frasi adiacenti cambia almeno uno fra
     (a) soggetto della riflessione (il narratore vs Marullino vs
     un'idea generale), (b) tempo verbale dominante (presente gnomico
     vs imperfetto durativo vs trapassato vs condizionale), (c)
     funzione retorica (sentenza generale vs autoaccusa vs ritratto vs
     metafora). Sono Reflection distinte, anche se vicine nel testo.
     Esempio: un paragrafo meditativo che articola un sogno filosofico
     (sentenza ipotetica) + una sentenza gnomica sul contatto + una
     descrizione condizionale della dinamica amorosa + una diagnosi
     finale produce QUATTRO Reflection, non una.
   - ACCORPARE quando: il testo costruisce un'UNICA voce esortativa o
     un'UNICA apostrofe lirica che si distende su più frasi parallele
     senza cambio di soggetto, tempo o funzione. Esempio canonico:
     un'apostrofe all'anima in punto di morte che si articola su tre
     movimenti coordinati (caratterizzazione + invito a guardare +
     esortazione finale) è UNA Reflection-invocazione, non tre.
     Segnale linguistico dell'unità: la voce è la stessa, il tempo
     verbale è omogeneo (presente esortativo continuato), il soggetto
     dell'enunciazione non cambia.

8. <!-- STRENGTHENED 0.4.2 -->
   I nodi isolati sono legittimi e VANNO ESTRATTI. Una Person o un Place
   nominato nel chunk senza partecipare attivamente alla scena è
   informazione del grafo, non rumore: verrà ricongiunto a contesto da
   altri chunk in stadio 4. Estrarlo come nodo senza archi (o, per
   Person, con `INVOLVES role=mentioned` se compare in un Event della
   scena come semplice riferimento) è il comportamento corretto.

   Casi tipici da NON saltare:
   - Person: filosofi o figure storiche citati come termine di paragone
     o di confronto ("come Catone", "i sistemi di Filolao e Ipparco",
     "Aristarco di Samo che io ho prescelto"), predecessori richiamati
     di passaggio ("gli ultimi anni del regno di Traiano"), figure della
     rete familiare nominate senza azione nel chunk corrente
     ("la fronte madida di mio padre"). Sono Person nodo, sempre.
   - Place: regioni geografiche entro cui si svolge la scena (Spagna,
     Oriente, Giudea, "fra il Giordano e il mare") anche quando il
     focus è su un sub-luogo specifico; fiumi/montagne nominati come
     cornice della scena ("il Nilo" mentre Adriano lo risale,
     "il Danubio" mentre evoca i prigionieri). Sono Place nodo, sempre.

   Test pratico: se rimuovi il nome dal chunk, il chunk perde
   un'informazione verificabile sulla biografia del soggetto? Sì →
   estrai il nodo, anche senza archi. NON saltare per "economia": la
   copertura nominale è valore del grafo.

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
    if edge.get("type") == "INVOLVES" and edge.get("role") is not None:
        out["role"] = edge["role"]
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
# Subject profile: rendering del profilo del soggetto biografico per il prompt.
#
# Il SYSTEM_PROMPT contiene il placeholder `{SUBJECT_PROFILE}` dentro un blocco
# <subject_profile>...</subject_profile>. Quel placeholder viene riempito a
# runtime in `build_request_payload` con la stringa restituita da
# `render_subject_profile()`.
#
# Architettura (vedi schema.Subject e PIPELINE.md):
# - I dati del soggetto sono iniettati da configurazione, non estratti dal
#   modello. Per Yourcenar vivono in `data/subject_profile_adriano.json`;
#   per il caso clinico (futuro) vivranno in un file analogo del paziente.
# - Il caller di stadio 3 (`stage_3-2_extract.py`) carica il JSON, chiama
#   `render_subject_profile()`, e passa la stringa risultante a
#   `build_request_payload(..., subject_profile=<rendered_str>)`.
# - Il prompt vede il profilo come contesto di ambientazione, NON come
#   campi da estrarre. La regola "non duplicare il profilo come nodi" è
#   già scritta nel SYSTEM_PROMPT (sezione "Profilo del soggetto").
#
# Robustezza: la funzione gestisce campi mancanti silenziosamente. Un campo
# assente o vuoto viene omesso dalla stringa renderizzata, non sostituito
# con "None" o "—". Questo perché il prompt è in italiano naturale e i
# riempitivi disturberebbero il modello.
# -----------------------------------------------------------------------------


def render_subject_profile(profile: dict) -> str:
    """
    Rende un dict di profilo soggetto in una stringa testuale leggibile,
    pensata per essere inserita nel SYSTEM_PROMPT al posto del placeholder
    `{SUBJECT_PROFILE}`.

    Campi attesi (tutti opzionali tranne `name`):
    - `name`         : forma canonica italiana del nome.
    - `birth_year`   : int.
    - `death_year`   : int. Omesso → "vivente".
    - `birth_place`  : str.
    - `surnames`     : list[str] di nomi formali/storici.
    - `aliases`      : list[str] di varianti incontrate o attese nel testo.
    - `description`  : str, sintesi 1-2 frasi del ruolo storico.
    - `biographical_notes` : str, paragrafo libero con contesto esteso.

    Solleva ValueError se manca il `name`: senza nome canonico il profilo
    non ha senso e il prompt diventerebbe ambiguo.

    Esempio output (campi vuoti omessi):

        Nome canonico: Adriano
        Anni: 76–138 d.C.
        Luogo di nascita: Italica, Spagna Betica
        Nomi formali: Publio Elio Adriano, Adriano Augusto
        Altre varianti dal testo: Cesare, l'imperatore
        Ruolo: Imperatore romano dal 117 al 138 d.C., narratore-protagonista.
        Note biografiche:
        Imperatore romano (regno 117-138 d.C.), gens Aelia, ...
    """
    name = profile.get("name")
    if not name or not str(name).strip():
        raise ValueError(
            "subject_profile: campo 'name' mancante o vuoto; "
            "il profilo del soggetto richiede almeno il nome canonico."
        )

    lines: list[str] = [f"Nome canonico: {name}"]

    birth_year = profile.get("birth_year")
    death_year = profile.get("death_year")
    if birth_year is not None and death_year is not None:
        lines.append(f"Anni: {birth_year}–{death_year} d.C.")
    elif birth_year is not None:
        lines.append(f"Anno di nascita: {birth_year} d.C. (vivente)")
    elif death_year is not None:
        lines.append(f"Anno di morte: {death_year} d.C.")

    birth_place = profile.get("birth_place")
    if birth_place:
        lines.append(f"Luogo di nascita: {birth_place}")

    surnames = profile.get("surnames") or []
    if surnames:
        lines.append(f"Nomi formali: {', '.join(surnames)}")

    aliases = profile.get("aliases") or []
    if aliases:
        lines.append(f"Altre varianti dal testo: {', '.join(aliases)}")

    description = profile.get("description")
    if description:
        lines.append(f"Ruolo: {description}")

    biographical_notes = profile.get("biographical_notes")
    if biographical_notes:
        lines.append("Note biografiche:")
        lines.append(biographical_notes)

    return "\n".join(lines)


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
                                "Theme", "Reflection", "Work", "Era",
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
                                "CAUSED", "FOLLOWS", "OCCURS_IN",
                            ],
                        },
                        "description": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": list(INVOLVES_ROLES),
                            "description": (
                                "Ruolo della Person nell'Event. "
                                "OBBLIGATORIO se type='INVOLVES', "
                                "VIETATO per ogni altro tipo di arco. "
                                "Valori canonici: "
                                "'protagonist' (Person che agisce o subisce "
                                "l'Event come soggetto centrale), "
                                "'participant' (presente alla scena con "
                                "ruolo attivo ma non centrale), "
                                "'mentioned' (citata di sfuggita, senza "
                                "agire nell'Event). La regola di coerenza "
                                "type↔role è enforced lato Pydantic dopo "
                                "il parsing del tool output."
                            ),
                        },
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
    esempi). Vedi ADR-013. TTL "1h": rispetto al default 5min costa 2x base
    in scrittura (vs 1.25x), ma copre sia run sync lunghe (310 chunk × ~30s)
    sia le run batch che possono richiedere parecchi minuti tra primo e
    ultimo chunk.
    """
    block: dict = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": "ok",
    }
    if with_cache:
        block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
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
# con TTL `1h` (vs default 5min). Motivazione del 1h:
#   - run sync su 310 chunk = ~30-90 min in totale, la 5min cache verrebbe
#     invalidata e ri-scritta più volte;
#   - run batch (Message Batches API): le request sono concorrenti, ma fra
#     la submission e il completamento di tutti i chunk può passare
#     parecchio (1h tipico, fino a 24h teorici); con cache 5min molte
#     request perdono l'hit, con 1h la cache resta calda per tutta la run.
# Costo aggiuntivo del 1h: cache write = 2x base (vs 1.25x del 5min). Per
# il nostro prefisso ~14k token su Sonnet 4.6 ($3/Mtok input, o $1.50 batch)
# l'extra è dell'ordine di pochi centesimi sulla full run — trascurabile.
#
#   #1: sul SYSTEM_PROMPT (qui sotto).
#   #2: sul tool_result dell'ultimo few-shot (in build_messages).
# Effetto: ~14k token di prompt fisso pagati pieni una sola volta, poi letti
# da cache al ~10% del costo. La sola parte non cachata per chunk è il
# chunk_user_message corrente.
# -----------------------------------------------------------------------------

_SUBJECT_PROFILE_PLACEHOLDER = "{SUBJECT_PROFILE}"


def build_request_payload(
    chunk_id: str,
    chunk_text: str,
    model: str,
    subject_profile: str,
) -> dict:
    """
    Restituisce un dict pronto per `client.messages.create(**payload)`.
    Il caller aggiunge max_tokens, temperature, eventuali altri parametri.

    Parametri
    ---------
    chunk_id, chunk_text : il chunk corrente da estrarre.
    model : id del modello (es. "claude-sonnet-4-6-20260101").
    subject_profile : stringa testuale del profilo del soggetto della
        biografia, già renderizzata (vedi `render_subject_profile()`).
        Viene sostituita al placeholder `{SUBJECT_PROFILE}` dentro il
        SYSTEM_PROMPT prima dell'invio.

    Nota sul caching
    ----------------
    Il SYSTEM_PROMPT con il profilo sostituito è IDENTICO attraverso tutti
    i chunk di uno stesso run (stesso soggetto), quindi il cache_control
    breakpoint #1 funziona normalmente: write una volta sola, hit per
    tutti i chunk successivi. Cambia solo cambiando soggetto fra run
    diversi.
    """
    if not subject_profile or not subject_profile.strip():
        raise ValueError(
            "subject_profile è vuoto: passare la stringa renderizzata da "
            "render_subject_profile() o un equivalente caricato dal "
            "profilo del soggetto."
        )
    if _SUBJECT_PROFILE_PLACEHOLDER not in SYSTEM_PROMPT:
        raise RuntimeError(
            f"placeholder {_SUBJECT_PROFILE_PLACEHOLDER!r} non trovato nel "
            "SYSTEM_PROMPT: il template è stato modificato in modo "
            "incompatibile con la sostituzione runtime del profilo."
        )

    system_text = SYSTEM_PROMPT.replace(
        _SUBJECT_PROFILE_PLACEHOLDER, subject_profile
    )

    return {
        "model": model,
        "system": [
            {
                "type": "text",
                "text": system_text,
                # cache breakpoint #1 (vedi nota TTL sopra)
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "tools": [EXTRACTION_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_extraction"},
        "messages": build_messages(chunk_id, chunk_text),
    }
