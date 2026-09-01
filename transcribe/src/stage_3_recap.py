"""Stadio 3: riassunto LLM del dialogo (stadio 2).

Supporta LM Studio locale (OpenAI-compatible) e DeepSeek API (chiave in `.env` in root del repo).

Esecuzione (env attivo, cwd = transcribe/):
    python src/stage_3_recap.py
    python src/stage_3_recap.py --backend local
    python src/stage_3_recap.py --backend deepseek --only user

Output:
    data/stage_3_recap/<nome>.md
    data/stage_3_recap/recap_log.json
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import Literal

from common import format_duration, get_logger, iso_now, write_json, write_text
from config import REPO_ROOT, TranscribeConfig, load_config

STAGE_VERSION = "0.1.0"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ENV_PATH = REPO_ROOT / ".env"
logger = get_logger(__name__)

Backend = Literal["local", "deepseek"]
_THINK_OPEN = re.compile(r"<\s*think\s*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Env / prompt
# -----------------------------------------------------------------------------


def load_dotenv() -> None:
    """Carica la `.env` in root del repo (senza sovrascrivere variabili già impostate)."""
    if not ENV_PATH.is_file():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip().strip('"').strip("'")


def load_system_prompt(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"Prompt di sistema non trovato: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"Prompt di sistema vuoto: {path}")
    return text


# -----------------------------------------------------------------------------
# LLM
# -----------------------------------------------------------------------------


def _openai_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit(
            "openai non installato. Installa con: pip install openai"
        ) from exc
    return OpenAI


def strip_thinking(text: str) -> str:
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


def resolve_lmstudio_model(client, requested: str | None, base_url: str) -> str:
    if requested:
        return requested
    try:
        models = client.models.list()
        ids = [m.id for m in models.data]
    except Exception as exc:
        raise SystemExit(
            f"LM Studio non raggiungibile su {base_url} ({exc}). "
            "Avvia il server (tab Developer) e carica qwen3-14b."
        ) from exc
    if not ids:
        raise SystemExit(f"Nessun modello caricato in LM Studio su {base_url}.")
    return ids[0]


def call_llm(
    *,
    backend: Backend,
    cfg: TranscribeConfig,
    system_prompt: str,
    dialogue: str,
) -> tuple[str, str]:
    user_message = (
        "Ecco la trascrizione del dialogo da riassumere:\n\n"
        f"{dialogue.strip()}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    OpenAI = _openai_client()
    extra_body: dict | None = None

    if backend == "local":
        client = OpenAI(base_url=cfg.lm_studio_url, api_key="lm-studio")
        model_id = resolve_lmstudio_model(
            client, cfg.lm_studio_model, cfg.lm_studio_url
        )
        disable_thinking = True
    else:
        load_dotenv()
        api_key = env("DEEPSEEK_API_KEY")
        model_id = env("DEEPSEEK_MODEL") or "deepseek-chat"
        if not api_key:
            raise SystemExit(f"DEEPSEEK_API_KEY mancante in {ENV_PATH}")
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        extra_body = {"thinking": {"type": "disabled"}}
        disable_thinking = True

    kwargs: dict = {
        "model": model_id,
        "messages": messages,
        "temperature": cfg.recap_temperature,
        "max_tokens": cfg.recap_max_tokens,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body

    logger.info("Chiamata LLM (%s, modello %s)…", backend, model_id)
    resp = client.chat.completions.create(**kwargs)
    content = (resp.choices[0].message.content or "").strip()
    if disable_thinking:
        content = strip_thinking(content)
    if not content:
        raise SystemExit("Il modello ha restituito un riassunto vuoto.")
    return content, model_id


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------


def build_output(recap: str, dialogue_md: str, source_label: str) -> str:
    return (
        f"# Riassunto — {source_label}\n\n"
        f"{recap.strip()}\n\n"
        f"---\n\n"
        f"{dialogue_md.rstrip()}\n"
    )


def list_dialogue_files(stage2_dir: Path, only_stem: str | None) -> list[Path]:
    if only_stem is not None:
        path = stage2_dir / f"{only_stem}.md"
        if not path.is_file():
            raise SystemExit(
                f"Dialogo non trovato: {path}. Esegui prima lo stadio 2."
            )
        return [path]
    files = sorted(p for p in stage2_dir.glob("*.md"))
    if not files:
        raise SystemExit(f"Nessun dialogo in {stage2_dir}. Esegui prima lo stadio 2.")
    return files


def recap_one(
    dialogue_path: Path, cfg: TranscribeConfig, backend: Backend, out_dir: Path
) -> dict:
    dialogue_md = dialogue_path.read_text(encoding="utf-8")
    system_prompt = load_system_prompt(cfg.recap_prompt_file)
    recap, model_id = call_llm(
        backend=backend,
        cfg=cfg,
        system_prompt=system_prompt,
        dialogue=dialogue_md,
    )

    stem = dialogue_path.stem
    source_label = stem
    first_line = dialogue_md.splitlines()[0] if dialogue_md else ""
    if first_line.startswith("# Dialogo"):
        source_label = first_line.removeprefix("# Dialogo —").strip() or stem

    out_path = out_dir / f"{stem}.md"
    write_text(out_path, build_output(recap, dialogue_md, source_label))
    logger.info("[%s] riassunto -> %s", stem, out_path.name)
    return {
        "file": stem,
        "backend": backend,
        "model": model_id,
        "recap_chars": len(recap),
    }


# -----------------------------------------------------------------------------
# Prompt interattivo
# -----------------------------------------------------------------------------


def prompt_recap_yes_no() -> bool:
    answer = input("\nGenerare il riassunto LLM del dialogo? [y/N]: ").strip().lower()
    return answer in ("y", "yes", "s", "si", "sì")


def prompt_backend() -> Backend:
    print("\nBackend LLM:")
    print("  1) LM Studio (locale, qwen3-14b)")
    print("  2) DeepSeek API")
    while True:
        choice = input("Scelta [1/2]: ").strip()
        if choice == "1":
            return "local"
        if choice == "2":
            return "deepseek"
        print("Inserisci 1 o 2.")


def prompt_and_run(cfg: TranscribeConfig, only_stem: str | None = None) -> list[dict] | None:
    if not prompt_recap_yes_no():
        print("Stadio 3 saltato.")
        return None
    backend = prompt_backend()
    return run(cfg, only_stem=only_stem, backend=backend)


# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------


def run(
    cfg: TranscribeConfig,
    only_stem: str | None = None,
    backend: Backend | None = None,
) -> list[dict]:
    if backend is None:
        backend = prompt_backend()

    dialogue_files = list_dialogue_files(cfg.stage2_dir, only_stem)
    out_dir = cfg.stage3_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = iso_now()
    t0 = time.perf_counter()
    results = [recap_one(p, cfg, backend, out_dir) for p in dialogue_files]
    elapsed = time.perf_counter() - t0

    write_json(
        out_dir / "recap_log.json",
        {
            "stage_version": STAGE_VERSION,
            "started_at": started_at,
            "finished_at": iso_now(),
            "elapsed_s": round(elapsed, 1),
            "backend": backend,
            "recap_prompt_file": str(cfg.recap_prompt_file),
            "results": results,
        },
    )

    print(
        f"\nStadio 3 completato in {format_duration(elapsed)}: "
        f"{len(results)} riassunti -> {out_dir}"
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Stadio 3: riassunto LLM del dialogo.")
    parser.add_argument("--config", type=Path, default=None, help="Path a config.yaml")
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Processa solo questo file (nome senza estensione). Default: tutti.",
    )
    parser.add_argument(
        "--backend",
        choices=("local", "deepseek"),
        default=None,
        help="Backend LLM (default: chiede a terminale).",
    )
    args = parser.parse_args()
    run(load_config(args.config), args.only, args.backend)


if __name__ == "__main__":
    main()
