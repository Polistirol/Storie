#!/usr/bin/env python3
"""API web — GraphRAG + streaming (LM Studio o API remota via --use_API)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from rag.env import api_provider_label, list_api_provider_names, load_inference_env
from rag.session import InferenceSession, open_session

# --- session store (history per browser tab) ---

_engine: InferenceSession | None = None
_config_path: str | None = None
_use_api: str | None = None
_histories: dict[str, list[dict[str, str]]] = {}
_lock = threading.Lock()
_verbose = False

log = logging.getLogger("adriano.api")


def _init_engine() -> None:
    global _engine
    if _engine is not None:
        return
    load_inference_env()
    _engine = open_session(_config_path, quiet=True, use_api=_use_api)


def _backend_label() -> str:
    session = _engine
    if session is None or session.llm is None:
        return "LLM"
    if _use_api and session.llm:
        return f"{api_provider_label(session.llm_provider or _use_api)} ({session.llm.model_id})"
    return f"LM Studio ({session.llm.model_id})"


def _get_engine() -> InferenceSession:
    if _engine is None:
        raise RuntimeError("Motore inference non inizializzato")
    return _engine


def _retrieval_payload(session: InferenceSession, session_id: str) -> dict[str, Any]:
    r = session.last_result
    if r is None:
        return {"session_id": session_id, "central_node_id": None}

    payload: dict[str, Any] = {
        "session_id": session_id,
        "central_node_id": r.central_node_id,
        "graph_node_count": len(r.graph_node_ids),
    }
    if r.timings:
        payload["timings"] = {
            "total_s": round(r.timings.total_s, 3),
            "embed_s": round(r.timings.embed_s, 3),
            "search_s": round(r.timings.search_s, 3),
            "graph_s": round(r.timings.graph_s, 3),
        }
    return payload


def _log_turn(message: str, session_id: str, session: InferenceSession) -> None:
    r = session.last_result
    log.info("Tu: %s", message)
    if not r:
        return
    log.info(
        "Retrieval — %d chunk · nodo centrale: %s · %d nodi grafo",
        len(r.chunks),
        r.central_node_id or "—",
        len(r.graph_node_ids),
    )
    if _verbose:
        for i, ch in enumerate(r.chunks[:5], 1):
            log.info("  chunk[%d] %s score=%.3f", i, ch.chunk_id, ch.score)
        ranked = session.graph.nodes_ranked_by_chunks(r.chunks)[:8]
        for nid, sc, via in ranked:
            log.info("    %.3f %s ← %s", sc, nid, ", ".join(via[:2]))
        log.info("  graph: %s", sorted(r.graph_node_ids))
        if r.timings:
            log.info(
                "  timings: total=%.2fs embed=%.2fs search=%.3fs graph=%.3fs",
                r.timings.total_s,
                r.timings.embed_s,
                r.timings.search_s,
                r.timings.graph_s,
            )
        if session.llm:
            log.info("  model: %s", session.llm.model_id)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chat_stream(message: str, session_id: str) -> Iterator[str]:
    session = _get_engine()
    sid = session_id or str(uuid.uuid4())

    with _lock:
        saved_history = session.history
        session.history = list(_histories.get(sid, []))
        try:
            _, context = session.retrieve(message)
            _log_turn(message, sid, session)
            yield _sse("retrieval", _retrieval_payload(session, sid))

            parts: list[str] = []
            for token in session.stream_answer(message, context=context):
                parts.append(token)
                yield _sse("token", {"t": token})

            answer = "".join(parts).strip()
            if not answer:
                raise RuntimeError(
                    "Risposta vuota dal modello — cronologia non aggiornata."
                )

            if _verbose:
                log.info("Adriano: %s", answer)

            _histories[sid] = list(session.history)
            yield _sse("done", {"session_id": sid})
        except Exception as exc:
            session.history = saved_history
            log.exception("Errore turno chat")
            yield _sse("error", {"message": str(exc)})
        finally:
            session.history = saved_history


# --- FastAPI ---


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _init_engine()
    yield


app = FastAPI(title="Adriano GraphRAG", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None


class ClearRequest(BaseModel):
    session_id: str


@app.get("/api/health")
def health() -> dict[str, Any]:
    session = _get_engine()
    model = session.llm.model_id if session.llm else None
    provider = session.llm_provider or (_use_api if _use_api else "lmstudio")
    return {
        "status": "ok",
        "model": model,
        "provider": provider,
        "backend": _backend_label(),
        "sessions": len(_histories),
    }


@app.post("/api/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message vuoto")

    return StreamingResponse(
        _chat_stream(message, req.session_id or ""),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/clear")
def clear_chat(req: ClearRequest) -> dict[str, bool]:
    with _lock:
        _histories.pop(req.session_id, None)
    return {"ok": True}


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Adriano GraphRAG — API web locale")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--config", default=None, help="path config.yaml")
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log retrieval completo e risposta LLM (come chat.py --verbose)",
    )
    load_inference_env()
    api_examples = ", ".join(list_api_provider_names()) or "groq, deepseek"
    ap.add_argument(
        "--use_API",
        dest="use_api",
        metavar="NAME",
        default=None,
        help=f"provider API remoto (es. {api_examples} — deve coincidere con *_NAME_ID in .env)",
    )
    args = ap.parse_args()

    global _config_path, _verbose, _use_api
    _config_path = args.config
    _verbose = args.verbose
    _use_api = args.use_api

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    _init_engine()

    print(f"API pronta su http://{args.host}:{args.port} [{_backend_label()}]")
    print("Endpoint: POST /api/chat  ·  POST /api/chat/clear  ·  GET /api/health")
    print("Log chat/retrieval: questo terminale (usa --verbose per il dettaglio completo)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
