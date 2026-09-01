# Inference — agente GraphRAG

Solo **inferenza**: **domanda → chunk (BGE-M3) + grafo arricchito (1 hop) → Qwen/Groq**.

Questa cartella **non costruisce indici**. La costruzione dell'indice RAG è lo
**Stadio 6** della pipeline (`Adriano_graph/src/stage_6-1_index.py`, con
checkpoint `stage_6-2_health_checkup.py`). L'inferenza si limita a **consumare**
gli artefatti degli stadi conclusi:

- l'indice vettoriale da `Adriano_graph/data/stage_6/1_index/`
- il grafo arricchito da `Adriano_graph/data/stage_5/5_transforms/enriched_graph.json`

Legge tutto senza modificarlo. Vedi ADR-029 in `Adriano_graph/PIPELINE.md`.

---

## Prerequisiti

1. **Ambiente Python 3.10+** con dipendenze:

   Env unificato **`adriano-kg`** (consigliato — copre Adriano_graph, inference e visual/tools):

   ```powershell
   cd Storie
   micromamba create -f environment.yml
   micromamba activate adriano-kg
   ```

   Oppure script:

   ```powershell
   .\envs\install_kg.ps1
   ```

   venv alternativo (dalla root):

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. **BGE-M3** locale — path in `config.yaml` (`embed_model`). Default:

   `C:/Users/Pc-Gaming/Documents/models/embeddings/bge-m3`

3. **LM Studio** — carica Qwen3 8B, avvia il server (tab Developer, porta 1234).

4. **Indice dello Stadio 6 già costruito**. Dalla cartella `Adriano_graph/`:

   ```powershell
   micromamba activate adriano-kg
   cd Adriano_graph
   python src/stage_6-1_index.py            # → data/stage_6/1_index/
   python src/stage_6-2_health_checkup.py   # → data/stage_6/2_health_checkup/dashboard.html
   ```

   Rilancia `stage_6-1_index.py` quando cambia `chunks.json` o il grafo arricchito.

5. **Dati pipeline** (prodotti dagli stadi conclusi):

   - indice: `Adriano_graph/data/stage_6/1_index/` (vectors, meta, chunk_texts, manifest)
   - chunk: `Adriano_graph/data/stage_2/chunks.json`
   - grafo: `Adriano_graph/data/stage_5/5_transforms/enriched_graph.json`

---

## Configurazione

Modifica `config.yaml` se serve:

| Chiave | Significato |
|--------|-------------|
| `chunks_path` / `graph_path` | Input testo e grafo (stadi conclusi) |
| `index_dir` | Indice dello Stadio 6 (`../Adriano_graph/data/stage_6/1_index`) |
| `embed_model` / `embed_device` | BGE-M3 e `cuda` o `cpu` (deve combaciare con lo Stadio 6) |
| `top_k_chunks` | Quanti chunk recuperare (default 5) |
| `max_graph_nodes` | Cap nodi grafo dopo 1 hop (default 25) |
| `lmstudio_url` | Default `http://localhost:1234/v1` |
| `lmstudio_model` | `null` = primo modello caricato, oppure id esplicito |
| `disable_thinking` | `true` = `/no_think` + filtro blocchi thinking in output |

---

## Pipeline — passi in ordine

> L'indice si costruisce nello **Stadio 6** (vedi Prerequisiti, punto 4), non qui.
> I passi sotto sono solo inferenza.

### 1. Verificare il retrieval (senza LLM)

Utile per controllare chunk e nodi prima di chiamare Qwen:

```powershell
python ask.py "Come mai sei andato dal medico Ermogene stamattina?" --verbose --no-llm
```

Controlla che i `chunk_id` recuperati siano sensati e che compaiano nodi/archi plausibili.

---

### 2. Domanda con risposta (LM Studio acceso)

```powershell
python ask.py "Che rapporto avevi con Boristene?" --verbose
```

Salvare log retrieval + risposta:

```powershell
python ask.py "Come vivi la morte che si avvicina?" --save-log data/smoke/manual.json
```

---

### 3. Smoke test (5 domande, end-to-end)

> Diverso dall'health_checkup dello Stadio 6: questo è uno smoke *dell'agente*
> (retrieval + risposta LLM). La convalida deterministica dell'indice (copertura
> nodi, smoke retrieval senza LLM) è in `stage_6-2_health_checkup.py`.


```powershell
python smoke_test.py
```

Scrive un JSON per domanda in `data/smoke/` + `*_summary.json`.

Solo retrieval:

```powershell
python smoke_test.py --no-llm
```

---

### 4. Chat interattiva (consigliato)

Sessione persistente: **indice, grafo, embedder e LM Studio si caricano una sola volta**.
Ogni messaggio fa retrieval fresco; la cronologia multi-turno resta in memoria.

```powershell
python chat.py
```

Opzioni:

```powershell
python chat.py --verbose          # log retrieval a ogni turno (stderr)
python chat.py --no-stream        # risposta intera, senza streaming
```

#### Groq API (più veloce del locale)

Crea `inference/.env` (non committare — già in `.gitignore`):

```env
GROQ_NAME_ID=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile
```

Il nome passato a `--use_API` **deve coincidere** con `GROQ_NAME_ID`; il modello
effettivo è sempre `GROQ_MODEL` dal file env.

```powershell
python chat.py --use_API groq
```

Con Groq non si usa `/no_think` (specifico Qwen locale). LM Studio resta il default
senza flag.

Comandi in chat:

| Comando | Azione |
|---------|--------|
| `/help` | Elenco comandi |
| `/quit` | Esci |
| `/clear` | Azzera cronologia |
| `/verbose` | Toggle log retrieval |
| `/retrieval` | Chunk/nodi dell'ultimo turno |

**Thinking (Qwen3):** con `disable_thinking: true` in config (default), ogni prompt
termina con `/no_think` e l'output filtra eventuali blocchi di ragionamento del modello.

**Latenza tra domande:** il collo di bottiglia per turno è il **retrieval locale**
(embedding BGE-M3 + lettura testi), non Groq/LM Studio. Ottimizzazioni attive:

- testi chunk in RAM (non si rilegge `chunks.json` a ogni domanda)
- `chunk_texts.json` prodotto dallo Stadio 6 — se manca, rigenera con `stage_6-1_index.py`
- `max_chunk_chars: 1200` tronca i chunk nel prompt → primo token LLM più veloce

Per misurare i tempi:

```powershell
python chat.py --use_API groq --timing
```

Se `embed` domina (>0.3s), resta il costo GPU di BGE-M3 per domanda. Se `contesto` è
grande (>8 KB), abbassa `max_chunk_chars` o `top_k_chunks` in config.

**Qualità risposta:** config attuale orientata al ricco (8 chunk interi, 40 nodi grafo).
Follow-up conversazionali («ti piaceva quindi?») usano query arricchita + prompt
anti-ripetizione + meno chunk nel prompt. Se le risposte restano povere, alza
`temperature` (0.4–0.5) o `max_tokens`.

---

## Chat web (grafo + streaming)

Interfaccia browser in `visual/index.html` con pannello chat a destra e evidenziazione
del sotto-grafo recuperato a ogni domanda.

### Avvio (due terminali)

**Terminale 1 — LLM** (scegli uno):

- **LM Studio (default):** carica Qwen3 8B, avvia server Developer (porta 1234).
- **Groq:** crea `inference/.env` (vedi sezione Groq sotto); non serve LM Studio.

**Terminale 2 — API GraphRAG:**

```powershell
micromamba activate adriano-kg
cd inference
pip install fastapi uvicorn   # se non già installati

# LM Studio (default)
python server.py --port 8000
python server.py --port 8000 --verbose

# Groq API (stesso frontend visual/)
python server.py --port 8000 --use_API deepseek
python server.py --port 8000 --use_API deepseek --verbose
```

Retrieval (BGE-M3), nodo centrale, layout `maps/` e chat browser funzionano
identici con LM Studio o Groq; cambia solo chi genera la risposta.

**Dove vedere i log:** nel terminale dove gira `python server.py` (non quello di `npx serve`).
Ogni domanda stampa domanda utente + seed/nodi evidenziati; con `--verbose` anche
chunk_ids, lista completa node_ids e testo risposta.

Il primo avvio carica BGE-M3, indice e grafo (~10–30 s). Endpoint:

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/api/health` | Stato, `provider`, `model`, `backend` |
| POST | `/api/chat` | Domanda → SSE (retrieval + token stream) |
| POST | `/api/chat/clear` | Azzera cronologia (`session_id`) |

**Terminale 3 — frontend statico:**

```powershell
cd visual
npx --yes serve -p 3000
```

Apri `http://localhost:3000`. La chat chiama l'API su `http://127.0.0.1:8000`.
Porta diversa: `?api=http://127.0.0.1:9000` nell'URL del browser.

### Contratto SSE (`POST /api/chat`)

Body JSON:

```json
{ "message": "Che rapporto avevi con Boristene?", "session_id": "uuid-opzionale" }
```

Eventi (ordine):

1. `retrieval` — `central_node_id`, `graph_node_count`, `timings`, `session_id`
2. `token` — `{ "t": "..." }` (ripetuto)
3. `done` — `{ "session_id": "..." }`
4. `error` — `{ "message": "..." }` (se fallisce)

La risposta arriva in streaming; il nodo centrale aggiorna il pannello sinistro e il layout `maps/`.
Follow-up multi-turno: stesso `session_id` finché resti sulla pagina (refresh = nuova chat).
Pulsante **Clear** ≡ `/clear` in CLI.

### File coinvolti

```
inference/server.py     # FastAPI + SSE (LM Studio o --use_API groq)
visual/chat.js          # client chat
visual/index.html       # chat, nodo centrale, layout maps/
```

`chat.py` resta la CLI di riferimento; la logica RAG è in `rag/session.py`.

---

## Cosa fa ogni modulo

```
inference/
├── config.yaml          # path e parametri (index_dir → Stadio 6)
├── ask.py               # domanda singola (opz. --stream)
├── chat.py              # chat interattiva + streaming
├── server.py            # API web FastAPI + SSE (chat browser)
├── smoke_test.py        # 5 domande batch (end-to-end con LLM)
└── rag/
    ├── session.py       # sessione persistente (caricamento una tantum + check manifest)
    ├── index.py         # CARICA e interroga l'indice dello Stadio 6 (load + search)
    ├── graph_store.py   # grafo JSON + 1 hop
    ├── retriever.py     # chunk top-K + espansione grafo
    ├── prompts.py       # system prompt Adriano prima persona
    └── llm.py           # LM Studio + streaming + filtro thinking
```

> La costruzione dell'indice è in `Adriano_graph/src/stage_6-1_index.py` (Stadio 6),
> non più in `inference/`.

Flusso interno:

1. Embed domanda → cosine sui chunk indicizzati  
2. Nodi con `provenance.chunk_id` nei chunk recuperati  
3. Espansione **1 hop** sui vicini; cap a `max_graph_nodes`  
4. Contesto = testi chunk + nodi/relazioni compatti  
5. Qwen risponde in prima persona, solo dal contesto  

---

## Troubleshooting

| Problema | Cosa fare |
|----------|-----------|
| `Indice non trovato` | Da `Adriano_graph/`: `python src/stage_6-1_index.py` |
| `LM Studio non raggiungibile` | Avvia server locale, verifica porta 1234 |
| `sentence-transformers non installato` | `pip install -r requirements.txt` |
| CUDA OOM su embed | In `config.yaml` imposta `embed_device: cpu` |
| `manifest indice assente` | Indice pre-Stadio 6: rigenera con `stage_6-1_index.py` |
| `il grafo è cambiato dopo la build` | Rigenera l'indice (Stadio 6) per riallineare |
| Risposta inventata | Riduci `top_k_chunks`, controlla log con `--verbose --no-llm` |
| Contesto troppo lungo | Abbassa `max_graph_nodes` o `max_description_chars` |

---

## Prossimi passi

- Indicizzazione semantica dei nodi/temi del grafo (oggi l'indice è sui soli chunk;
  macro-temi e gerarchia `SPECIALIZES` sono raggiunti solo via chunk) — eventuale `stage_6-3`
- Neo4j per query Cypher più ricche
- Valutazione su `dataset_qa`
- Review umana post-enrich (coda `stage_5/6_health_checkup/review_queue.json`) prima dell'indice definitivo

Fatto: indice formalizzato come **Stadio 6** della pipeline (build + health_checkup);
`inference/` ridotto alla sola inferenza che consuma gli artefatti (ADR-029).
