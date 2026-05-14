"""
Stadio 1: cleaning deterministico del testo grezzo (regex + YAML).

Esecuzione (cwd = Adriano_graph/):
    python src/stage_1_clean.py

Path e regole si configurano con le costanti in cima; nessun argparse.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# -----------------------------------------------------------------------------
# Costanti modificabili
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_REL = "data/stage_0/raw_text.txt"
RULES_REL = "config/cleaning_rules.yaml"
OUTPUT_DIR_REL = "data/stage_1"
STAGE_VERSION = "0.1.0"
HIGH_MATCH_WARNING_THRESHOLD = 100

# Ispezione: offset e contesto per report JSON
INSPECTION_CONTEXT_RADIUS = 40
CLEANING_LOG_MAX_SAMPLES = 5
INSPECTION_MAX_SAMPLES = 10

# Lettere e punteggiatura permessa (ispezione rare_chars); il resto è segnalato.
_ALLOWED_ITALIAN_LETTERS_LOWER = set("abcdefghijklmnopqrstuvwxyzàèéìòù")
_ALLOWED_ITALIAN_LETTERS_UPPER = set("ABCDEFGHIJKLMNOPQRSTUVWXYZÀÈÉÌÒÙ")
_ALLOWED_DIGITS = set("0123456789")
_ALLOWED_WHITESPACE = {" ", "\t", "\n", "\r", "\f", "\v"}
_ALLOWED_PUNCT = set(".,;:!?\"'«»()[]—–-…")

ALLOWED_CHARS = (
    _ALLOWED_ITALIAN_LETTERS_LOWER
    | _ALLOWED_ITALIAN_LETTERS_UPPER
    | _ALLOWED_DIGITS
    | _ALLOWED_WHITESPACE
    | _ALLOWED_PUNCT
)

# Ripetizione: 3+ occorrenze dello stesso segno fra quelli elencati.
_REPEATED_PUNCT_CLASS = r'[.,;:!?\'"«»()—–\-…]'


# -----------------------------------------------------------------------------
# Utilità
# -----------------------------------------------------------------------------


def abort(reason: str) -> None:
    print(reason, file=sys.stderr)
    raise SystemExit(1)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_dumps_stable(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def context_around(text: str, start: int, end: int, radius: int = INSPECTION_CONTEXT_RADIUS) -> str:
    """Estrae il contesto visivo intorno a [start, end) per log e report (prima di modifiche)."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return text[lo:hi]


# -----------------------------------------------------------------------------
# Caricamento regole
# -----------------------------------------------------------------------------


def load_rules(rules_path: Path) -> tuple[list[dict[str, Any]], dict[str, re.Pattern[str]]]:
    """
    Legge il dizionario YAML e compila le regex: serve fallire subito se il pattern
    è invalido o mancano campi, così non si applicano regole a metà.
    """
    if not rules_path.is_file():
        abort(f"File regole mancante: {rules_path}")

    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        abort(f"YAML mal formato ({rules_path}): {e}")

    if not isinstance(raw, dict) or "rules" not in raw:
        abort(f"YAML: atteso mapping con chiave 'rules' in {rules_path}")

    rules_list = raw["rules"]
    if not isinstance(rules_list, list):
        abort(f"YAML: 'rules' deve essere una lista in {rules_path}")

    required = ("id", "pattern", "replacement", "enabled")
    rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    compiled: dict[str, re.Pattern[str]] = {}

    for i, item in enumerate(rules_list):
        if not isinstance(item, dict):
            abort(f"YAML: ogni regola deve essere un mapping (indice {i}) in {rules_path}")
        for key in required:
            if key not in item:
                abort(
                    f"YAML: regola indice {i} priva del campo obbligatorio '{key}' in {rules_path}"
                )

        rule_id = item["id"]
        if not isinstance(rule_id, str) or not rule_id.strip():
            abort(f"YAML: 'id' regola indice {i} deve essere stringa non vuota in {rules_path}")
        if rule_id in seen_ids:
            abort(f"YAML: id regola duplicato {rule_id!r} in {rules_path}")
        seen_ids.add(rule_id)

        pattern = item["pattern"]
        replacement = item["replacement"]
        enabled = item["enabled"]

        if not isinstance(pattern, str):
            abort(f"YAML: regola {rule_id!r}: 'pattern' deve essere stringa")
        if not isinstance(replacement, str):
            abort(f"YAML: regola {rule_id!r}: 'replacement' deve essere stringa")
        if not isinstance(enabled, bool):
            abort(f"YAML: regola {rule_id!r}: 'enabled' deve essere booleano")

        if pattern == "":
            if enabled:
                abort(
                    f"YAML: regola {rule_id!r} ha pattern vuoto e enabled=true: "
                    "impossibile applicarla in sicurezza"
                )
        else:
            try:
                compiled[rule_id] = re.compile(pattern)
            except re.error as e:
                abort(f"Regex invalida per regola {rule_id!r}: {e}")

        rules.append(item)

    return rules, compiled


# -----------------------------------------------------------------------------
# Applicazione regole
# -----------------------------------------------------------------------------


def apply_rules(
    raw_text: str,
    rules: list[dict[str, Any]],
    compiled: dict[str, re.Pattern[str]],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """
    Applica le regole in ordine di file. Ogni regola vede il testo già modificato
    dalle precedenti: così il log resta riproducibile e deterministico.
    """
    text = raw_text
    substitutions_by_rule: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []
    total = 0

    for rule in rules:
        rule_id = rule["id"]
        if not rule["enabled"]:
            substitutions_by_rule[rule_id] = {"count": 0, "samples": []}
            continue

        pattern = compiled[rule_id]
        matches = list(pattern.finditer(text))
        rule_total = len(matches)

        samples: list[dict[str, Any]] = []
        if rule_total > HIGH_MATCH_WARNING_THRESHOLD:
            warnings.append(
                {
                    "code": "high_match_count",
                    "rule_id": rule_id,
                    "count": rule_total,
                    "message": (
                        "Regola con molte sostituzioni: possibile falso positivo, "
                        "verificare il pattern nel YAML."
                    ),
                }
            )

        # Sostituire da destra a sinistra mantiene validi gli offset dei match precedenti.
        for m in sorted(matches, key=lambda x: x.start(), reverse=True):
            start, end = m.start(), m.end()
            matched = m.group(0)
            repl = m.expand(rule["replacement"])
            ctx = context_around(text, start, end)
            rec = {
                "rule_id": rule_id,
                "offset": start,
                "context": ctx,
                "matched": matched,
                "replacement": repl,
            }
            if len(samples) < CLEANING_LOG_MAX_SAMPLES:
                samples.append(rec)
            text = text[:start] + repl + text[end:]

        substitutions_by_rule[rule_id] = {"count": rule_total, "samples": samples}
        total += rule_total

    summary = {"total_substitutions": total, "substitutions_by_rule": substitutions_by_rule}
    return text, summary, warnings


# -----------------------------------------------------------------------------
# Ispezione (solo diagnostica, sul grezzo stage_0)
# -----------------------------------------------------------------------------


def _inspection_sample(
    text: str, start: int, end: int, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "offset": start,
        "context": context_around(text, start, end),
    }
    if extra:
        out.update(extra)
    return out


def inspect_text(text: str) -> dict[str, Any]:
    """
    Cerca pattern sospetti senza mutare il testo: l'utente decide se aggiungere regole YAML.
    """
    findings: dict[str, Any] = {}

    # digit_in_word: token con cifre e lettere nello stesso "parola" (\w); esclude puri numeri.
    diw_re = re.compile(r"\w*\d+\w*")
    diw_samples: list[dict[str, Any]] = []
    diw_count = 0
    for m in diw_re.finditer(text):
        g = m.group(0)
        if re.fullmatch(r"\d+", g):
            continue
        diw_count += 1
        if len(diw_samples) < INSPECTION_MAX_SAMPLES:
            diw_samples.append(
                _inspection_sample(text, m.start(), m.end(), {"matched": g})
            )
    findings["digit_in_word"] = {"count": diw_count, "samples": diw_samples}

    # lonely_digit_sequence: 1-3 cifre delimitate da spazi o inizio/fine (prosa).
    lonely_re = re.compile(r"(?:^|\s)(\d{1,3})(?=\s|$)", re.MULTILINE)
    lonely_samples: list[dict[str, Any]] = []
    lonely_count = 0
    for m in lonely_re.finditer(text):
        lonely_count += 1
        if len(lonely_samples) < INSPECTION_MAX_SAMPLES:
            inner = m.group(1)
            # offset del gruppo cifre (non dello spazio iniziale se presente)
            g_start = m.start(1)
            g_end = m.end(1)
            lonely_samples.append(
                _inspection_sample(text, g_start, g_end, {"matched": inner})
            )
    findings["lonely_digit_sequence"] = {"count": lonely_count, "samples": lonely_samples}

    # rare_chars: per carattere non ammesso, conteggio globale + esempi.
    rare_char_counter: Counter[str] = Counter()
    rare_samples: list[dict[str, Any]] = []
    for i, ch in enumerate(text):
        if ch in ALLOWED_CHARS:
            continue
        rare_char_counter[ch] += 1
        if len(rare_samples) < INSPECTION_MAX_SAMPLES:
            rare_samples.append(_inspection_sample(text, i, i + 1, {"char": ch}))
    findings["rare_chars"] = {
        "count": sum(rare_char_counter.values()),
        "chars": dict(sorted(rare_char_counter.items(), key=lambda x: (-x[1], x[0]))),
        "samples": rare_samples,
    }

    # repeated_punctuation: 3+ ripetizioni dello stesso segno tra quelli elencati.
    rep_re = re.compile(rf"({_REPEATED_PUNCT_CLASS})\1{{2,}}")
    rep_samples: list[dict[str, Any]] = []
    rep_count = 0
    for m in rep_re.finditer(text):
        rep_count += 1
        if len(rep_samples) < INSPECTION_MAX_SAMPLES:
            rep_samples.append(
                _inspection_sample(text, m.start(), m.end(), {"matched": m.group(0)})
            )
    findings["repeated_punctuation"] = {"count": rep_count, "samples": rep_samples}

    # isolated_capitals: maiuscola singola dopo minuscola (mezzo frase), prima spazio/punteggiatura.
    iso_re = re.compile(
        r"(?<=[a-zàèéìòù])([A-ZÀÈÉÌÒÙ])(?=[\s.,;:!?'»\)\]\-—–…])"
    )
    iso_samples: list[dict[str, Any]] = []
    iso_count = 0
    for m in iso_re.finditer(text):
        iso_count += 1
        if len(iso_samples) < INSPECTION_MAX_SAMPLES:
            cap = m.group(1)
            iso_samples.append(
                _inspection_sample(text, m.start(1), m.end(1), {"matched": cap})
            )
    findings["isolated_capitals"] = {"count": iso_count, "samples": iso_samples}

    return findings


# -----------------------------------------------------------------------------
# Scrittura output
# -----------------------------------------------------------------------------


def write_outputs(
    out_dir: Path,
    cleaned_text: str,
    cleaning_log: dict[str, Any],
    inspection_report: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cleaned_text.txt").write_text(cleaned_text, encoding="utf-8")
    (out_dir / "cleaning_log.json").write_text(
        json_dumps_stable(cleaning_log), encoding="utf-8"
    )
    (out_dir / "inspection_report.json").write_text(
        json_dumps_stable(inspection_report), encoding="utf-8"
    )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> None:
    started_at = iso_now()
    input_path = PROJECT_ROOT / INPUT_REL
    rules_path = PROJECT_ROOT / RULES_REL
    out_dir = PROJECT_ROOT / OUTPUT_DIR_REL

    if not input_path.is_file():
        abort(f"Input mancante: {input_path}")

    raw_text = input_path.read_text(encoding="utf-8")
    rules, compiled = load_rules(rules_path)

    rules_loaded = len(rules)
    rules_enabled = sum(1 for r in rules if r["enabled"])

    cleaned_text, sub_summary, warnings = apply_rules(raw_text, rules, compiled)
    finished_at = iso_now()

    cleaning_log: dict[str, Any] = {
        "stage_version": STAGE_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "rules_file": RULES_REL,
        "rules_loaded": rules_loaded,
        "rules_enabled": rules_enabled,
        "total_substitutions": sub_summary["total_substitutions"],
        "substitutions_by_rule": sub_summary["substitutions_by_rule"],
        "warnings": warnings,
    }

    inspection_report: dict[str, Any] = {
        "stage_version": STAGE_VERSION,
        "generated_at": finished_at,
        "source_file": INPUT_REL,
        "total_chars": len(raw_text),
        "findings": inspect_text(raw_text),
    }

    write_outputs(out_dir, cleaned_text, cleaning_log, inspection_report)

    # Report stdout (no esempi lunghi: restano nei JSON)
    in_chars = len(raw_text)
    out_chars = len(cleaned_text)
    print(f"Caratteri input:  {in_chars}")
    print(f"Caratteri output: {out_chars}")
    if in_chars != out_chars:
        print(
            "Nota: lunghezze diverse perché almeno una regola abilitata ha modificato il testo."
        )
    else:
        print("Input e output hanno la stessa lunghezza (nessun cambiamento netto o regole inattive).")

    print(f"Regole caricate: {rules_loaded}, abilitate: {rules_enabled}")
    print(f"Sostituzioni totali: {sub_summary['total_substitutions']}")
    print("Ispezione (conteggi per categoria):")
    findings = inspection_report["findings"]
    for key in (
        "digit_in_word",
        "lonely_digit_sequence",
        "rare_chars",
        "repeated_punctuation",
        "isolated_capitals",
    ):
        fc = findings[key]["count"]
        print(f"  {key}: {fc}")

    print(
        "Offset nei campi 'samples' del cleaning_log: "
        "indice nel testo subito prima della sostituzione "
        "(dopo le altre regole elencate prima nel YAML)."
    )
    print("File scritti:")
    print(f"  {out_dir / 'cleaned_text.txt'}")
    print(f"  {out_dir / 'cleaning_log.json'}")
    print(f"  {out_dir / 'inspection_report.json'}")


if __name__ == "__main__":
    main()
