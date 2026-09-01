from __future__ import annotations

import os
from pathlib import Path

_INFERENCE_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _INFERENCE_ROOT.parent
_ENV_PATH = _REPO_ROOT / ".env"


def _parse_env_file(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_inference_env() -> None:
    """Carica la `.env` in root del repo (senza sovrascrivere variabili già in shell)."""
    if not _ENV_PATH.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_PATH, override=False)
    except ImportError:
        pass
    _parse_env_file(_ENV_PATH)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip().strip('"').strip("'")


def resolve_embed_model(raw: str) -> str:
    """Id Hugging Face, oppure path assoluto / relativo alla root del repo."""
    text = raw.strip()
    p = Path(text)
    if p.is_absolute():
        return str(p)
    candidate = (_REPO_ROOT / p).resolve()
    if candidate.exists():
        return str(candidate)
    return text


# Registro provider API: aggiungere una voce per ogni backend OpenAI-compatible.
# Ogni provider usa in .env: {PREFIX}_NAME_ID, {PREFIX}_API_KEY, {PREFIX}_MODEL
_API_PROVIDER_SPECS: dict[str, tuple[str, str, str]] = {
    "groq": ("GROQ_NAME_ID", "GROQ_API_KEY", "GROQ_MODEL"),
    "deepseek": ("DEEPSEEK_NAME_ID", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
}

_API_PROVIDER_LABELS: dict[str, str] = {
    "groq": "Groq API",
    "deepseek": "DeepSeek API",
}


def list_api_provider_names() -> list[str]:
    """Nomi passabili a --use_API (valori *_NAME_ID definiti in .env)."""
    load_inference_env()
    names: list[str] = []
    for _key, (name_env, _key_env, _model_env) in _API_PROVIDER_SPECS.items():
        val = _env(name_env)
        if val:
            names.append(val)
    return names


def api_provider_label(provider_id: str) -> str:
    return _API_PROVIDER_LABELS.get(provider_id, provider_id)


def resolve_api_provider(provider_name: str) -> tuple[str, str, str]:
    """
    Valida `provider_name` contro un *_NAME_ID in .env e ritorna
    (provider_id, api_key, model_id).

    Uso: server.py --use_API deepseek  (dove deepseek == DEEPSEEK_NAME_ID in .env)
    """
    load_inference_env()
    needle = provider_name.strip().lower()
    registered: list[str] = []

    for provider_id, (name_env, key_env, model_env) in _API_PROVIDER_SPECS.items():
        expected = _env(name_env)
        if not expected:
            continue
        registered.append(expected)
        if needle != expected.lower():
            continue

        api_key = _env(key_env)
        model = _env(model_env)
        if not api_key:
            raise SystemExit(f"{key_env} mancante in {_ENV_PATH}")
        if not model:
            raise SystemExit(f"{model_env} mancante in {_ENV_PATH}")
        return provider_id, api_key, model

    if not registered:
        raise SystemExit(
            f"Nessun provider API configurato in {_ENV_PATH}. "
            "Copia .env.example → .env in root del repo e compila almeno "
            "un *_NAME_ID / *_API_KEY / *_MODEL."
        )
    raise SystemExit(
        f"Provider API {provider_name!r} non riconosciuto. "
        f"Valori attesi (--use_API): {', '.join(registered)!r}"
    )


def resolve_groq_provider(provider_name: str) -> tuple[str, str]:
    """Retrocompatibilità: ritorna solo (api_key, model_id) per Groq."""
    provider_id, api_key, model = resolve_api_provider(provider_name)
    if provider_id != "groq":
        raise SystemExit(f"Provider {provider_name!r} non è Groq.")
    return api_key, model
