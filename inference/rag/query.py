from __future__ import annotations

# Seguiti conversazionali brevi: arricchiscono la query di retrieval.
_FOLLOW_UP_MAX_WORDS = 5
_FOLLOW_UP_HINTS = (
    "quindi",
    "allora",
    "davvero",
    "vero",
    "perché",
    "perche",
    "come mai",
    "e tu",
    "ti piace",
    "ti piaceva",
    "dimmi di più",
    "di più",
    "ancora",
    " anche ",
    " lui ",
    " lei ",
    " questo ",
    " questa ",
)


def _has_follow_up_hint(question: str) -> bool:
    q = f" {question.lower()} "
    return any(h in q for h in _FOLLOW_UP_HINTS)


def is_follow_up(question: str, history: list[dict[str, str]]) -> bool:
    """Vero solo per seguiti brevi/conversazionali, non per ogni domanda con history."""
    if not history:
        return False
    words = question.split()
    if _has_follow_up_hint(question):
        return True
    return len(words) <= _FOLLOW_UP_MAX_WORDS


def expand_retrieval_query(
    question: str,
    history: list[dict[str, str]],
) -> str:
    """
    Per seguiti tipo «ti piaceva molto quindi?» embedda anche la domanda precedente,
    così il retrieval resta ancorato al tema (Antinoo, non chunk generici).
    """
    if not is_follow_up(question, history):
        return question
    prev_user = history[-2]["content"] if len(history) >= 2 else ""
    if not prev_user:
        return question
    return f"{prev_user.strip()} — {question.strip()}"
