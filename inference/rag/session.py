from __future__ import annotations

import sys
import time
from typing import Iterator

from rag.config import AppConfig, load_config
from rag.embedder import Embedder
from rag.graph_store import GraphStore
from rag.index import ChunkIndex, load_manifest
from rag.llm import ChatClient, Provider, create_api_chat, create_lmstudio_chat
from rag.env import api_provider_label, resolve_api_provider
from rag.prompts import build_chat_messages, build_messages
from rag.query import expand_retrieval_query, is_follow_up
from rag.retriever import RetrievalResult, Retriever, RetrievalTimings, format_context


class InferenceSession:
    """
    Sessione persistente: carica indice, grafo, embedder e LLM una sola volta.
    Ogni turno esegue retrieval fresco; la history conserva solo Q&A pulite.
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        quiet: bool = False,
        connect_llm: bool = True,
        use_api: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.quiet = quiet
        self.history: list[dict[str, str]] = []
        self.last_result: RetrievalResult | None = None
        self.last_context: str = ""
        self.last_timings: RetrievalTimings | None = None
        self.llm: ChatClient | None = None
        self.llm_provider: Provider | None = None

        t0 = time.perf_counter()
        self._log("Caricamento indice chunk…")
        self.index = ChunkIndex.load(cfg.index_dir, chunks_path=cfg.chunks_path)
        if not self.index.has_texts():
            self._log("Caricamento testi chunk (fallback da chunks.json)…")
            self.index.load_texts(cfg.chunks_path)

        self._log("Caricamento grafo…")
        self.graph = GraphStore(cfg.graph_path)
        self._check_manifest(cfg)

        self._log("Caricamento embedder (BGE-M3)…")
        self.embedder = Embedder(cfg.embed_model, device=cfg.embed_device)
        self.embedder.encode_one("warmup")

        self.retriever = Retriever(cfg, self.index, self.graph, self.embedder)

        if connect_llm:
            if use_api:
                provider_id, api_key, model = resolve_api_provider(use_api)
                self._log(f"Connessione {api_provider_label(provider_id)}…")
                self.llm = create_api_chat(
                    provider_id,
                    api_key,
                    model,
                    temperature=cfg.temperature,
                    max_tokens=cfg.max_tokens,
                )
                self.llm_provider = provider_id
                label = f"{api_provider_label(provider_id)} — `{self.llm.model_id}`"
            else:
                self._log("Connessione LM Studio…")
                self.llm = create_lmstudio_chat(
                    cfg.lmstudio_url,
                    cfg.lmstudio_model,
                    temperature=cfg.temperature,
                    max_tokens=cfg.max_tokens,
                    disable_thinking=cfg.disable_thinking,
                )
                self.llm_provider = "lmstudio"
                label = f"LM Studio — `{self.llm.model_id}`"
            elapsed = time.perf_counter() - t0
            self._log(f"Pronto — {label} ({elapsed:.1f}s). Digita una domanda o /help.")
        else:
            elapsed = time.perf_counter() - t0
            self._log(f"Retrieval pronto ({elapsed:.1f}s, LLM disabilitato).")

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def _check_manifest(self, cfg: AppConfig) -> None:
        """Provenienza indice (Stadio 6) + avviso se il grafo è cambiato dopo la build."""
        manifest = load_manifest(cfg.index_dir)
        if not manifest:
            self._log(
                "  ⚠ manifest indice assente: indice forse pre-Stadio 6. "
                "Rigenera con Adriano_graph/src/stage_6-1_index.py."
            )
            return
        graph_meta = manifest.get("graph") or {}
        run = graph_meta.get("source_run")
        built = manifest.get("timestamp", "?")
        self._log(f"  indice Stadio 6 ({built}) · grafo run={run}")

        expected_sha = graph_meta.get("sha256")
        if expected_sha and cfg.graph_path.is_file():
            try:
                import hashlib

                h = hashlib.sha256()
                with cfg.graph_path.open("rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block)
                if h.hexdigest() != expected_sha:
                    self._log(
                        "  ⚠ il grafo è cambiato dopo la build dell'indice: "
                        "rigenera lo Stadio 6 per riallineare (stage_6-1_index.py)."
                    )
            except OSError:
                pass

    def clear_history(self) -> None:
        self.history.clear()

    def retrieve(self, question: str) -> tuple[RetrievalResult, str]:
        query = expand_retrieval_query(question, self.history)
        top_k = self.cfg.top_k_chunks
        result = self.retriever.retrieve(query, top_k=top_k, central_question=question)
        context = format_context(
            result,
            self.graph,
            self.cfg.max_description_chars,
            max_chunk_chars=self.cfg.max_chunk_chars,
        )
        self.last_result = result
        self.last_context = context
        self.last_timings = result.timings
        return result, context

    def log_timings(self) -> None:
        t = self.last_timings
        if not t:
            return
        ctx_kb = len(self.last_context.encode("utf-8")) // 1024
        print(
            f"⏱ retrieval {t.total_s:.2f}s "
            f"(embed {t.embed_s:.2f}s, search {t.search_s:.3f}s, "
            f"graph {t.graph_s:.3f}s, format {t.format_s:.3f}s) "
            f"| contesto ~{ctx_kb} KB",
            file=sys.stderr,
        )

    def _prompt_disable_thinking(self) -> bool:
        if self.llm_provider in ("groq", "deepseek"):
            return False
        return self.cfg.disable_thinking

    def _messages_for(self, question: str, context: str) -> list[dict[str, str]]:
        kw = {"disable_thinking": self._prompt_disable_thinking()}
        if self.history:
            return build_chat_messages(
                question,
                context,
                self.history,
                follow_up=is_follow_up(question, self.history),
                **kw,
            )
        return build_messages(question, context, **kw)

    def answer(self, question: str, *, context: str | None = None) -> str:
        if self.llm is None:
            raise RuntimeError("LLM non connesso (sessione avviata con connect_llm=False)")
        if context is None:
            _, context = self.retrieve(question)
        messages = self._messages_for(question, context)
        text = self.llm.complete(messages)
        self._commit_turn(question, text)
        return text

    def stream_answer(
        self, question: str, *, context: str | None = None
    ) -> Iterator[str]:
        if self.llm is None:
            raise RuntimeError("LLM non connesso (sessione avviata con connect_llm=False)")
        if context is None:
            _, context = self.retrieve(question)
        messages = self._messages_for(question, context)
        parts: list[str] = []
        for token in self.llm.stream(messages):
            parts.append(token)
            yield token
        self._commit_turn(question, "".join(parts))

    def _commit_turn(self, question: str, answer: str) -> None:
        answer = answer.strip()
        if not answer:
            raise RuntimeError(
                "Il modello ha restituito una risposta vuota "
                "(probabile limite token o thinking mode)."
            )
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})


def open_session(
    config_path: str | None = None,
    *,
    quiet: bool = False,
    connect_llm: bool = True,
    use_api: str | None = None,
) -> InferenceSession:
    return InferenceSession(
        load_config(config_path),
        quiet=quiet,
        connect_llm=connect_llm,
        use_api=use_api,
    )
