# Proposta selezione chunk — few-shot e test set

Riconferma del lavoro precedente (`notebooks/chunk_selection_proposal.md`) alla luce dello schema 0.2.0 (Era set chiuso, INVOLVES con `role`, TRANSFORMS_INTO Person→Person). Il testo dei 310 chunk non è cambiato; i chunk già scelti restano tutti validi e in alcuni casi coprono meglio le nuove dimensioni dello schema (vedi note in coda alle voci). Token = `token_count` esatto da `stage_2/chunks.json`.

## Distribuzione osservata nei 310 chunk

- **Totale**: 310 chunk su 6 parti: p1 ANIMULA (25), p2 VARIUS (68), p3 TELLUS (64), p4 SAECULUM (70), p5 DISCIPLINA (61), p6 PATIENTIA (22).
- **Lunghezza** (cl100k_base): min 95, max 1265, **media 510**, **mediana 479**. Distribuzione unimodale con coda destra. Decili: 233, 321, 387, 429, 479, 535, 604, 697, 804.
- **Outlier corti** (<200 tk, 21 chunk): chiusura del libro `ch_0310`; raccordi/itinerari brevi `ch_0142 ch_0144 ch_0146 ch_0174 ch_0179`; scene-cornice ridottissime `ch_0206 ch_0208`; aforismi politici `ch_0257 ch_0289 ch_0292`; flash narrativi `ch_0093 ch_0108 ch_0112 ch_0122 ch_0290`.
- **Outlier lunghi** (>1000 tk, 11 chunk): grandi blocchi tra cronaca e meditazione: `ch_0026` (genealogia), `ch_0039` (libertà/servitù), `ch_0080` (sogni partici), `ch_0131 ch_0132` (politica), `ch_0193` (magia), `ch_0194`, `ch_0227` (esequie Antinoo), `ch_0253` (Giudea), `ch_0270` (Serviano), `ch_0286` (adozione di Marc'Aurelio).
- **Tipologie ricorrenti**: (a) scena narrativa storica densa con molti propri (campagne, cerimonie, viaggi); (b) riflessione astratta senza propri (tempo, morte, libertà, corpo, sonno); (c) descrizione di luogo che scivola in meditazione; (d) ritratto di Person con cammeo storico; (e) eventi rituali con datazione esplicita; (f) raccordi brevi di transizione tra fasi. Discorso diretto raro (~10-15 chunk): il libro è quasi tutto monologo retrospettivo in prima persona indirizzato a Marco.
- **Asimmetrie utili**: stati emotivi più contrastati in p4 (morte di Antinoo) e p6 (preparazione alla morte). Scene con piena struttura Event+Person+Place "canonica" soprattutto in p2 e p3. Riflessioni più pure (zero entità nominate) concentrate in p1 e in coda a p6. La voce narrante di p1 e p6 vive nell'Era *vecchiaia*; *adultità* domina p3-p5; *gioventù* domina p2; *infanzia* compare solo in cammei retrospettivi (p1, parte di p2).

---

## 1. Candidati few-shot (8)

Casi in cui la struttura "giusta" da estrarre — un Event di scena con dettagli interni dentro la sua descrizione, oppure una Reflection ancorata a Theme/Person/Place — è abbastanza nitida da poter essere annotata a mano senza ambiguità rilevanti. Insieme coprono: scene fattuali brevi, catene causali, scene con stati interni, place-centric, Reflection pura, Reflection ancorata a Place, evento istituzionale + Theme, scena finale con cast affollato. Coprono inoltre i tre `role` di INVOLVES (protagonist/participant/mentioned) in modo naturale, e includono almeno un caso adatto a mostrare TRANSFORMS_INTO Person→Person (ch_0103).

| chunk_id | tk | parte | categoria | motivazione |
|---|---|---|---|---|
| `ch_0092` | 266 | p2 | scena narrativa breve, evento focale canonico | Cremazione di Traiano a Selinunte. Un Event compatto, un Place chiaro, cast esplicito (Traiano protagonist; Plotina/Matidia/Attiano/Crito participant). I dettagli sensoriali (nube di fumo, lacrime di Matidia, volto impenetrabile di Plotina) sono interni alla scena: esempio paradigmatico di "tutto dentro l'Event". |
| `ch_0046` | 497 | p2 | scena narrativa storica con catena causale | Morte di Nerva → galoppata di Adriano → cena con Serviano → agguato → arrivo a Colonia → nomina a tribuno. Esempio per FOLLOWS/CAUSED tra Event consecutivi e per la distinzione protagonist/participant/mentioned (Nerva mentioned, Serviano participant ostile, Adriano protagonist). Era: *gioventù*. |
| `ch_0199` | 336 | p4 | scena narrativa con stato emotivo | Festino dopo la caccia al leone in onore di Antinoo. Stati interni espliciti (gratitudine, orgoglio, mondo "eroico") che vanno tenuti *dentro* la descrizione dell'Event: non promuovere "gratitudine" o "orgoglio" a nodi separati. |
| `ch_0167` | 730 | p4 | descrizione di luogo + attività ricorrente | Atene ricostruita, musica nella corte del cipresso. Il Place (Atene) è il baricentro; gli Event sono attività ricorrenti (cantieri, musica), non puntuali. Insegna ad ancorare una scena a un Place dominante senza esplodere in micro-eventi. Buon test anche per `mentioned` (Pericle, Silla, il Seleucide). |
| `ch_0016` | 281 | p1 | riflessione astratta pura | Sull'ora di sonno restituita. Zero propri, zero scene. Una Reflection coesa su un Theme (sonno/tempo/ristoro). Caso canonico "puro pensiero, niente Event". |
| `ch_0114` | 476 | p3 | riflessione tematica ancorata a un Place | Roma come idea sopravvivente. Reflection su Theme (immortalità della Res publica) radicata in Roma e in contrasto con Place morti (Tebe, Babilonia, Tiro). Insegna a distinguere Place come *oggetto* da Place come *materia* della riflessione, e a usare CONTRASTS_WITH fra Place o Theme. |
| `ch_0103` | 431 | p3 | evento istituzionale + motivazione interiore esplicita | Rifiuto dei titoli onorifici. Un Event istituzionale chiaro accompagnato dalla sua giustificazione interna ("diventare o essere il più possibile Adriano"). Buon caso per EMBODIES Event→Theme (autorità, disciplina del prestigio) e per accennare a TRANSFORMS_INTO Person→Person (Adriano che diventa pienamente sé) senza esagerare. |
| `ch_0309` | 389 | p6 | scena narrativa di chiusura, cast emotivo | Capezzale a Baia. Scena unica e densa con molte Person (Antonino mentioned a distanza; Cabria/Celere/Diotimo participant; Adriano protagonist; eco di Boristene). Stati emotivi contrastanti già dentro la descrizione. Era: *vecchiaia*. |

---

## 2. Candidati test set (13)

Disgiunti dal blocco precedente. Mescolano casi tipici e casi limite scelti per stressare l'estrattore lungo dimensioni diverse: lunghezza, densità di entità, astrazione, ambiguità referenziale, tono metaforico, role-assignment, salti temporali.

| chunk_id | tk | parte | categoria | motivazione |
|---|---|---|---|---|
| `ch_0310` | 102 | p6 | caso limite — chunk cortissimo + componimento in versi | "Animula vagula blandula". Poemetto finale, simbolico, indirizzato all'anima. Stressa il modello su poco testo e altissima densità simbolica: rischio di sovra-estrarre Theme/Reflection. |
| `ch_0206` | 95 | p4 | caso limite — chunk cortissimo, setup di scena | Canopo, la casa della maga. Il chunk più corto del corpus. È un setup (luogo + arrivo + figura) senza l'azione che seguirà nei chunk successivi: test su quanto Event "monco" estrarre e sul rischio di inventare protagonisti. |
| `ch_0026` | 1225 | p2 | caso limite — chunk lunghissimo + ritratto genealogico | Il nonno Marullino. Blocco enorme, monografico su una Person, con molte Person/Place satellite. Stressa la tentazione di esplodere in troppi nodi. Era: *infanzia* (uno dei rari ancoraggi disponibili). |
| `ch_0253` | 1265 | p5 | caso limite — chunk lunghissimo + alta densità di nomi propri | Insuccesso della guerra di Giudea. Il chunk più lungo, ~22 propri tra Person, Place, popoli. Mescola cronaca, autocritica e Theme politico-religioso. Stress massimo su come comprimere senza perdere relazioni e su come scegliere `role` (protagonist/participant/mentioned) quando i nomi sono molti. |
| `ch_0218` | 503 | p4 | scena interna densa + salti temporali + cast implicito | "Antinoo è morto". Un singolo Event interiore (la presa di coscienza) che richiama a catena Marullino, il padre, la madre, Traiano, Plotina, Attiano, compagni traci. Test su `mentioned` di massa e su ECHOES verso Event passati. |
| `ch_0011` | 560 | p1 | riflessione astratta pura, zero entità nominate | Sistema di conoscenza basato sull'erotica. Niente propri, niente scene, solo categorie e micro-esempi astratti (il tribuno, lo schiavo, il vecchio amico). Test sulla disciplina nel non promuovere figure generiche a Person. |
| `ch_0303` | 611 | p6 | caso limite — contenuto mistico/metaforico con citazioni | Riti per evocare Antinoo. Invocazioni rituali, citazioni latine e omeriche ("Audivi voces divinas", il fantasma di Patroclo). Confine sfumato fra esperienza interiore e Event: test sull'ambiguità Reflection vs Event. |
| `ch_0091` | 705 | p2 | riflessione storico-controfattuale + incertezza esplicita | La morte di Traiano: ciò che non saprò mai. Adriano riflette su un Event che non ha visto e che probabilmente è stato falsificato. Ipotesi alternative, ambiguità su cosa è "accaduto". Stress su come gestire fatti che il narratore stesso marca come incerti, e su `role` quando le presenze sono congetturali (Plotina, Crito, Fedima). |
| `ch_0222` | 776 | p4 | descrizione di luogo + atto simbolico puntuale | Adriano incide il suo nome sul Colosso di Memnone. Lunga cornice descrittiva (re dimenticati, geroglifici, Giulia Balilla, Servio Soave, Eumene, Panio) dentro cui si annida un piccolo Event puntuale (l'incisione). Test: il modello sa distinguere la cornice dalla scena che merita Event? |
| `ch_0084` | 583 | p2 | evento storico esterno + stato interno contrastante | La campagna parta di Traiano, vista da lontano. Doppio piano: cronaca della campagna (Babilonia, Ctesifonte, Caraci) in parallelo all'autocritica di Adriano. Test su come gestire un Event esterno + una Reflection di chi narra, senza fonderle. |
| `ch_0212` | 225 | p4 | evento rituale + datazione esplicita + prefigurazione | Primo giorno di Athir, anniversario della morte di Osiris. Data assoluta (Olimpiade 226, anno 2), Theme rituale, tono di prefigurazione (la morte di Antinoo è imminente). Test su Phase/datazione e su ECHOES anticipatori. |
| `ch_0301` | 781 | p6 | trasformazione tematica + dispersione geografica | Antinoo divenuto culto: assimilazioni divine multiple (Ermes, Bacco, Pan, Diana, Aristeo, cavaliere Trace) attraverso Place diversi (Delfi, Eleusi, Arcadia, Tivoli, Asia, Tracia). Test su TRANSFORMS_INTO/EMBODIES, e sul giudizio: quante divinità promuovere a Person/Theme e quanti Place nominare. |
| `ch_0292` | 149 | p6 | caso limite — chunk corto con citazione letteraria ambigua | Achille e Patroclo (chiusura della citazione, presumibilmente Arriano). Per il lettore funziona come specchio del rapporto Adriano-Antinoo. Test sull'ambiguità: l'estrattore tratta la citazione come oggetto di Reflection (su Achille) o come ECHOES verso Antinoo? |

---

## Note di metodo

- Le due liste sono disgiunte: 8 + 13 = 21 chunk distinti su 310.
- Coperture per parte: few-shot p1×1, p2×2, p3×2, p4×2, p6×1; test p1×1, p2×3, p4×5, p5×1, p6×3. Volutamente nessun few-shot da p5 (Discipline politiche, molto denso) per evitare di indurre il modello a inflazionare nodi; p5 entra solo come stress nel test set (`ch_0253`).
- Coperture per Era: few-shot include gioventù (`ch_0046`), adultità (`ch_0092 ch_0167 ch_0199 ch_0103`), vecchiaia (`ch_0016 ch_0114 ch_0309`); test include infanzia (`ch_0026`), vecchiaia (`ch_0310 ch_0303 ch_0292 ch_0301`), e adultità diffusa.
- Tutte le selezioni del documento precedente (`notebooks/chunk_selection_proposal.md`) restano valide: il testo dei chunk non è cambiato e le evoluzioni di schema (Era chiuso, INVOLVES.role, TRANSFORMS_INTO Person→Person) sono additive rispetto ai casi qui scelti.
