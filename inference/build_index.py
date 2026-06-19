#!/usr/bin/env python3
"""Costruisce l'indice vettoriale sui chunk (BGE-M3)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from rag.config import load_config
from rag.index import build_and_save


def main() -> None:
    ap = argparse.ArgumentParser(description="Embed chunk → indice locale")
    ap.add_argument("--config", default=None, help="path config.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    build_and_save(cfg)


if __name__ == "__main__":
    main()
