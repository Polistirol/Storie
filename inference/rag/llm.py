from __future__ import annotations

import re
from typing import Iterator, Literal, Optional

Provider = Literal["lmstudio", "groq", "deepseek"]

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

_API_BASE_URLS: dict[str, str] = {
    "groq": GROQ_BASE_URL,
    "deepseek": DEEPSEEK_BASE_URL,
}

_THINK_OPEN = re.compile(r"<\s*think\s*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)


def _openai_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit(
            "openai non installato. Da inference/: pip install -r requirements.txt"
        ) from exc
    return OpenAI


def make_lmstudio_client(base_url: str):
    OpenAI = _openai_client()
    return OpenAI(base_url=base_url, api_key="lm-studio")


def resolve_lmstudio_model_id(client, requested: Optional[str], base_url: str) -> str:
    if requested:
        return requested
    try:
        models = client.models.list()
        ids = [m.id for m in models.data]
    except Exception as exc:
        raise SystemExit(
            f"LM Studio non raggiungibile su {base_url} ({exc}). "
            "Avvia il server locale (tab Developer) e carica Qwen3 8B."
        ) from exc
    if not ids:
        raise SystemExit(f"Nessun modello caricato in LM Studio su {base_url}.")
    return ids[0]


def strip_thinking(text: str) -> str:
    """Rimuove blocchi … eventualmente emessi dal modello."""
    out: list[str] = []
    rest = text
    while rest:
        m = _THINK_OPEN.search(rest)
        if not m:
            out.append(rest)
            break
        out.append(rest[: m.start()])
        rest = rest[m.end() :]
        end = _THINK_CLOSE.search(rest)
        if not end:
            break
        rest = rest[end.end() :]
    return "".join(out).strip()


class ChatClient:
    """Client chat OpenAI-compatible (LM Studio, Groq, …)."""

    def __init__(
        self,
        *,
        client,
        model_id: str,
        provider: Provider,
        temperature: float,
        max_tokens: int,
        disable_thinking: bool = True,
        extra_body: dict | None = None,
    ) -> None:
        self.client = client
        self.model_id = model_id
        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking
        self.extra_body = extra_body or {}

    def _request_kwargs(
        self, messages: list[dict[str, str]], *, stream: bool
    ) -> dict:
        kw: dict = {
            "model": self.model_id,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if self.extra_body:
            kw["extra_body"] = self.extra_body
        return kw

    def complete(self, messages: list[dict[str, str]]) -> str:
        resp = self.client.chat.completions.create(
            **self._request_kwargs(messages, stream=False)
        )
        msg = resp.choices[0].message
        content = (getattr(msg, "content", None) or "").strip()
        if self.disable_thinking:
            content = strip_thinking(content)
        return content

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        response = self.client.chat.completions.create(
            **self._request_kwargs(messages, stream=True)
        )
        if not self.disable_thinking:
            for chunk in response:
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
            return
        yield from _stream_without_thinking(response)


def create_lmstudio_chat(
    base_url: str,
    model: Optional[str],
    *,
    temperature: float,
    max_tokens: int,
    disable_thinking: bool = True,
) -> ChatClient:
    client = make_lmstudio_client(base_url)
    model_id = resolve_lmstudio_model_id(client, model, base_url)
    return ChatClient(
        client=client,
        model_id=model_id,
        provider="lmstudio",
        temperature=temperature,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
    )


def create_api_chat(
    provider: str,
    api_key: str,
    model: str,
    *,
    temperature: float,
    max_tokens: int,
) -> ChatClient:
    base_url = _API_BASE_URLS.get(provider)
    if not base_url:
        raise ValueError(f"Provider API non supportato: {provider!r}")
    OpenAI = _openai_client()
    client = OpenAI(api_key=api_key, base_url=base_url)
    extra_body = None
    if provider == "deepseek":
        # v4-flash ha thinking ON di default: brucia max_tokens in reasoning_content
        # e può lasciare content vuoto. Per chat/RAG lo disabilitiamo esplicitamente.
        extra_body = {"thinking": {"type": "disabled"}}
    return ChatClient(
        client=client,
        model_id=model,
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
        disable_thinking=provider != "groq",
        extra_body=extra_body,
    )


def create_groq_chat(
    api_key: str,
    model: str,
    *,
    temperature: float,
    max_tokens: int,
) -> ChatClient:
    return create_api_chat(
        "groq",
        api_key,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


# Alias retrocompatibilità
LMStudioChat = create_lmstudio_chat


def _stream_without_thinking(response) -> Iterator[str]:
    in_think = False
    carry = ""
    for chunk in response:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if not piece:
            continue
        carry += piece
        while carry:
            if in_think:
                m = _THINK_CLOSE.search(carry)
                if not m:
                    carry = ""
                    break
                carry = carry[m.end() :]
                in_think = False
                continue

            m = _THINK_OPEN.search(carry)
            if not m:
                yield carry
                carry = ""
                break
            if m.start() > 0:
                yield carry[: m.start()]
            carry = carry[m.end() :]
            in_think = True


def chat(
    *,
    base_url: str,
    model: Optional[str],
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    disable_thinking: bool = True,
) -> tuple[str, str]:
    client = create_lmstudio_chat(
        base_url,
        model,
        temperature=temperature,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
    )
    return client.complete(messages), client.model_id
