# Dipendenze — Adriano_graph

Documento di riferimento per ricreare l'ambiente che copre la pipeline completa in `src/`, `tools/` e `neo4j/` (stadi 0 → 5). Gli script Python nella root del repo (`Storie/`) non entrano in questo elenco.

File correlato:

| File | Uso |
|---|---|
| `config/env.yml` | Env micromamba: Python 3.10 + tutte le dipendenze pip (core e opzionali) |

---

## Dipendenze dirette (pin in `config/env.yml`)

| Pacchetto | Usato da | Note |
|---|---|---|
| `pydantic` | `src/schema.py`, dedup, extract, embodies, neo4j import | Validazione grafo |
| `PyYAML` | `src/stage_1_clean.py` | Legge `config/cleaning_rules.yaml` |
| `pdfplumber` | `src/stage_0_extract_pdf.py` | Trascina `pdfminer.six`, `pypdfium2`, `pillow` |
| `tiktoken` | `src/stage_2_chunk.py` | Conteggio token per chunking |
| `anthropic` | `src/stage_3-2_extract.py` | Estrazione LLM (+ batch API) |
| `python-dotenv` | `src/stage_3-2_extract.py` | Carica `.env` se presente |
| `networkx` | `tools/extraction_analysis.py` | Metriche grafo; usato da `stage_3-3_health_checkup` |
| `plotly` | `tools/extraction_analysis.py` | Solo con `--html` / `--plotly` |
| `neo4j` | `neo4j/import_to_neo4j.py` | Import programmatico (in produzione spesso Neo4j Desktop) |
| `torch` | `src/stage_5-2a_theme_candidates.py` | CUDA 13.0 via `--extra-index-url` in `env.yml` |
| `numpy` | `src/stage_5-2a_theme_candidates.py` | Similarità / embedding |
| `sentence-transformers` | `src/stage_5-2a_theme_candidates.py` | Trascina `transformers`, `scikit-learn`, ecc. |
| `openai` | `src/stage_5-2b_theme_judge.py` | Judge LLM per consolidamento Theme |

Stadi 4 e health checkup 4–5 usano **solo stdlib** + moduli locali `src.*`.

---

## Cosa *non* serve (tipici install per errore)

`transformers`, `scikit-learn`, `scipy` arrivano come dipendenze transitive di `sentence-transformers` — è normale.

Pacchetti come `llama-index`, `openai-whisper`, `comfyui_*`, `matplotlib`, `jupyterlab`, ecc. **non sono richiesti** da `Adriano_graph/`: probabilmente da altri esperimenti nello stesso env.

---

## Ricreare un env pulito con micromamba

### Opzione A — nuovo env (consigliata, non cancella nulla)

```powershell
cd Adriano_graph
micromamba create -n adriano-kg-clean -f config/env.yml
micromamba activate adriano-kg-clean
```

Verifica rapida:

```powershell
python -c "import pydantic, yaml, pdfplumber, tiktoken, anthropic, networkx, torch, numpy, openai; from sentence_transformers import SentenceTransformer; print('ok', torch.version.cuda)"
```

### Opzione B — ripulire l'env esistente `adriano-kg`

Micromamba non ha un equivalente semplice di `pip uninstall` selettivo su decine di pacchetti. Il modo più sicuro:

1. Tieni `adriano-kg` com'è (backup implicito).
2. Crea `adriano-kg-clean` come sopra.
3. Quando sei sicuro, elimina il vecchio: `micromamba remove -n adriano-kg --all`

### Opzione C — aggiornare pip dentro l'env attivo

```powershell
micromamba activate adriano-kg
micromamba create -n adriano-kg -f config/env.yml
```

Oppure reinstalla da `config/env.yml` (opzione A). L'aggiornamento manuale **non rimuove** i pacchetti extra già installati; per un env davvero minimale serve l'opzione A.

---

## Variabili d'ambiente

| Variabile | Quando |
|---|---|
| `ANTHROPIC_API_KEY` | Stadio 3 (`stage_3-2_extract.py`) |
| `.env` in root repo `Storie/.env` | Caricato automaticamente da stage 3 se `python-dotenv` è installato |

---

## Aggiornare questo documento

Dopo aver cambiato import in `src/` o `tools/`, rigenera la lista:

```powershell
# dalla cartella Adriano_graph, con l'env attivo
python - <<'PY'
import ast, pathlib
root = pathlib.Path(".")
mods = set()
for p in list(root.glob("src/**/*.py")) + list(root.glob("tools/**/*.py")) + list(root.glob("neo4j/**/*.py")):
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names: mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
stdlib = {"__future__","argparse","collections","copy","dataclasses","datetime","enum","functools","html","importlib","json","logging","math","pathlib","re","statistics","sys","time","typing"}
third = sorted(m for m in mods if m not in stdlib and m not in {"src","schema","extraction_analysis"})
print("\n".join(third))
PY
```

Confronta l'output con la tabella sopra e aggiorna `config/env.yml` se serve.
