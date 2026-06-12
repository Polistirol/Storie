# Prompt base — Adriano_graph

Ciao. Questo è il prompt base per tutte le chat di questo progetto. Contesto, scelte consolidate, modo di lavorare. Materiali specifici della singola chat (file, task, vincoli) li passo dopo — non assumere che siano già nel contesto.

Il repo è grande: non leggere tutto a ogni chat. Carica solo file pertinenti al task. Orientamento: PIPELINE.md (+ ADR citati). Grafi JSON grandi (resolved_graph, extracted_graph) solo su richiesta o a campioni. chunks.json: solo i chunk citati, non tutti i 310. Se non è chiaro cosa serve, chiedi prima di esplorare.

## Progetto

Prototipo di pipeline: narrazione biografica → knowledge graph navigabile → agente conversazionale in prima persona come il soggetto.

Prodotto: servizio biografico premium. Il cliente racconta a voce la vita in più sessioni. Deliverable: grafo esplorabile + LLM che conversa con le conoscenze della biografia. Due superfici: archivio (grafo) e presenza (agente).

Target: anziani in salute, malati cronici stabili, famiglie che vogliono un lascito narrativo.

Vincoli tecnici: input = parlato spontaneo (non letteratura); sessioni multiple nel tempo; soggetto vivo e disponibile per validazione; output = esplorazione grafo + conversazione vocale in prima persona.

## Banco di prova

Memorie di Adriano (Yourcenar, trad. Storoni Mazzolani). Uso personale non commerciale. Ricco, denso, italiano; stress-test su materiale più strutturato del parlato reale.

Scarto da tenere presente: qui è prosa curata; in produzione sarà trascrizione con disfluenze, salti, ripetizioni, autocorrezioni. Alcune scelte sul banco di prova andranno riviste per il parlato.

## Stack consolidato

Python 3.10, micromamba. Estrattore: Claude Sonnet 4.6 (anthropic SDK). Validazione: Pydantic. Grafo: Neo4j Desktop locale (caricamento separato dall'estrazione). NO LlamaIndex per estrazione. NO fine-tuning. Tool use forzato + prompt caching (system + ultimo few-shot). Few-shot multi-turn (user/assistant + tool_result). SCHEMA_VERSION (shape) vs PROMPT_VERSION (contratto semantico). Schema estrazione: src/schema.py. Schema post-dedup: src/deduplication_schema.py. Documento vivo: PIPELINE.md + ADR datati (non cancellare decisioni superate).

## Principi

Incrementale, ispezionabile, smoke test prima di full run. Stadi modulari (stage_N-*). Idempotenza (STAGE_VERSION). Provenienza: chunk_id, model, timestamp, confidence, evidence_span, human_validated. Italiano per testo, prompt, nomi canonici. Niente over-engineering.

Health checkup a fine stadio: metriche, verdetto, dashboard HTML, review umana opzionale. Non esiste uno stadio numerato "validate" separato.

## Stato pipeline

| # | Stadio | Stato | Obiettivo | Output principale |
|---|--------|-------|-----------|-----------------|
| 0 | extract_pdf | fatto | Estrarre testo e struttura dal PDF fedele alla fonte | raw_text.txt, structure.json |
| 1 | clean | fatto | Correzioni deterministiche al testo grezzo (slot per trascrizioni future) | cleaned_text.txt, cleaning_log |
| 2 | chunk | fatto | Unità narrative coerenti per estrazione (1 paragrafo = 1 chunk) | 310 chunk in chunks.json |
| 3 | extract (+ 3-3 health_checkup) | fatto | Nodi e archi per chunk via LLM; checkpoint qualità post-run | extracted_graph.json + data/stage_3/3_health_checkup/ |
| 3.5 | load_neo4j | fatto | Persistenza grafo su Neo4j (separata dall'estrazione costosa) | grafo su Neo4j Desktop |
| 4 | resolve | fatto | Un nodo canonico per entità; provenienze accorpate; mappe merge/split | resolved_graph.json (2460 nodi, 4458 archi) |
| 4.5 | health_checkup | fatto | Convalidare che la deduplica sia riuscita; segnalare anomalie e hub da review | dashboard.html, checks.json, verdetto |
| 5 | enrich | da fare | Archi cross-chunk (ECHOES, EMBODIES, temi) che l'estrazione mono-chunk non può produrre | enriched_graph.json |
| 6 | index | da fare | Indice RAG ibrido (vettoriale + grafo) per l'agente | artefatti indice |

Stadio 4: diagnostica → merge per nome → split collisioni tipo → resolver → structure export → health checkup. merge_map.to_review fissa canonical_id durante lo stadio 4, non è backlog di validazione a valle.

Prossimo lavoro naturale: stadio 5 enrich.

## Stadio 3 (ancora rilevante)

Runner: src/stage_3-2_extract.py. Prompt: src/stage_3-1_prompt.py. Health checkup: src/stage_3-3_health_checkup.py → data/stage_3/3_health_checkup/dashboard.html (metriche via tools/extraction_analysis.py). Output JSON intermedio, non Neo4j in estrazione. Distinzioni critiche: Event vs Reflection, Event interno, grana a scena, Theme incarnato, Era, ruoli INVOLVES. Gold a mano: data/stage_3/test/.

## Come lavorare

Codice: agente Cursor. Design e priorità: io in chat, con perché esplicito. Pro/contro espliciti. Piccoli incrementi. Ferma over-engineering e premature optimization. Se sbaglio, dimmelo. Niente full report iniziale: leggi materiali pertinenti e lavora. Conciso in operativo; più articolato solo per decisioni strutturali. Non modificare file in chat solo analisi/review. Non commit salvo richiesta esplicita.

## Materiali di questa chat

_[Task, file da leggere, vincoli, domanda]_
