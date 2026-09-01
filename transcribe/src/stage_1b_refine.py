"""Stadio 1b: rifinitura degli speaker tramite embedding vocali (SpeechBrain ECAPA).

La diarizzazione di pyannote (stadio 1) a volte assorbe gli inserti brevi di uno
speaker nel turno dell'altro. Qui ri-valutiamo l'attribuzione confrontando
l'embedding vocale di ogni segmento con i profili A/B:

  1. Profili       - da campioni di enrollment (resources/speakers/<LABEL>/) se
                     presenti, altrimenti auto-derivati dalle regioni piu' lunghe e
                     sicure gia' separate da pyannote.
  2. Ri-assegnazione - per ogni segmento ASR (>= refine_min_segment_s) si assegna lo
                     speaker piu' simile, ma solo se il margine di similarita' e' netto.
  3. Split (opz.)  - un segmento lungo che contiene un inserto dell'altro speaker
                     (>= refine_window_s) viene spezzato.

Input:  data/stage_1_transcribe/<nome>.json  +  data/stage_0_ingest/<nome>.wav
Output: data/stage_1b_refine/<nome>.json (stesso schema, speaker rifiniti)

Esecuzione (env attivo, cwd = transcribe/):
    python src/stage_1b_refine.py
    python src/stage_1b_refine.py --only barbero
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional

import numpy as np

from common import (
    configure_model_cache,
    discover_audio_files,
    format_duration,
    get_logger,
    iso_now,
    patch_speechbrain_compat,
    quiet_noisy_logs,
    read_json,
    write_json,
)
from config import TranscribeConfig, load_config

STAGE_VERSION = "0.1.0"
SAMPLE_RATE = 16000
MIN_EMBED_SECONDS = 0.4          # sotto questa soglia l'audio e' troppo corto per un embedding
PROFILE_BUDGET_SECONDS = 25.0    # audio massimo per costruire un profilo auto-derivato
logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Embedding
# -----------------------------------------------------------------------------


class SpeakerEmbedder:
    """Wrapper su SpeechBrain ECAPA: audio (float32 16k) -> vettore L2-normalizzato."""

    def __init__(self, model_name: str, device: str, cache_dir: Path):
        import torch

        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError:  # speechbrain < 1.0
            from speechbrain.pretrained import EncoderClassifier  # type: ignore

        self._torch = torch
        savedir = cache_dir / "speechbrain" / model_name.replace("/", "__")
        savedir.mkdir(parents=True, exist_ok=True)

        # Su Windows senza Developer Mode i symlink falliscono (WinError 1314):
        # forziamo la strategia COPY per il fetch dei pesi, se disponibile.
        extra: dict = {}
        try:
            from speechbrain.utils.fetching import LocalStrategy

            extra["local_strategy"] = LocalStrategy.COPY
        except Exception:
            pass

        self.model = EncoderClassifier.from_hparams(
            source=model_name,
            savedir=str(savedir),
            run_opts={"device": device},
            **extra,
        )
        self.device = device

    def embed(self, audio: np.ndarray) -> Optional[np.ndarray]:
        if audio is None or len(audio) < int(MIN_EMBED_SECONDS * SAMPLE_RATE):
            return None
        torch = self._torch
        wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None, :]
        wav = wav.to(self.device)
        with torch.no_grad():
            emb = self.model.encode_batch(wav).reshape(-1).float().cpu().numpy()
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else None


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # gia' normalizzati


def mean_embedding(vectors: list[np.ndarray]) -> Optional[np.ndarray]:
    if not vectors:
        return None
    m = np.mean(np.stack(vectors, axis=0), axis=0)
    norm = np.linalg.norm(m)
    return m / norm if norm > 0 else None


# -----------------------------------------------------------------------------
# Audio
# -----------------------------------------------------------------------------


def load_audio_16k(path: Path) -> np.ndarray:
    """Decodifica qualsiasi formato in float32 mono 16k (via whisperx/ffmpeg)."""
    import whisperx

    return whisperx.load_audio(str(path))


def slice_audio(audio: np.ndarray, start: float, end: float) -> np.ndarray:
    i0 = max(0, int(start * SAMPLE_RATE))
    i1 = min(len(audio), int(end * SAMPLE_RATE))
    return audio[i0:i1]


# -----------------------------------------------------------------------------
# Profili speaker
# -----------------------------------------------------------------------------


def build_enrollment_profiles(
    cfg: TranscribeConfig, embedder: SpeakerEmbedder
) -> dict[str, np.ndarray]:
    """Profili da resources/speakers/<LABEL>/*.<audio>. Ritorna {label: embedding}."""
    profiles: dict[str, np.ndarray] = {}
    if not cfg.enrollment_dir.is_dir():
        return profiles
    for label_dir in sorted(p for p in cfg.enrollment_dir.iterdir() if p.is_dir()):
        files = discover_audio_files(label_dir, cfg.input_extensions)
        vectors: list[np.ndarray] = []
        for f in files:
            emb = embedder.embed(load_audio_16k(f))
            if emb is not None:
                vectors.append(emb)
        prof = mean_embedding(vectors)
        if prof is not None:
            profiles[label_dir.name] = prof
            logger.info("Profilo enrollment '%s' da %d campioni", label_dir.name, len(vectors))
    return profiles


def raw_speakers_in_order(segments: list[dict]) -> list[str]:
    order: list[str] = []
    for seg in segments:
        spk = seg.get("speaker")
        if spk and spk not in order:
            order.append(spk)
    return order


def build_cluster_centroid(
    segments: list[dict], raw_label: str, audio: np.ndarray, embedder: SpeakerEmbedder
) -> Optional[np.ndarray]:
    """Centroide di un cluster pyannote dai suoi segmenti piu' lunghi (budget limitato)."""
    segs = [
        s
        for s in segments
        if s.get("speaker") == raw_label
        and s.get("start") is not None
        and s.get("end") is not None
        and (s["end"] - s["start"]) >= MIN_EMBED_SECONDS
    ]
    segs.sort(key=lambda s: s["end"] - s["start"], reverse=True)
    vectors: list[np.ndarray] = []
    budget = 0.0
    for s in segs:
        emb = embedder.embed(slice_audio(audio, s["start"], s["end"]))
        if emb is not None:
            vectors.append(emb)
            budget += s["end"] - s["start"]
        if budget >= PROFILE_BUDGET_SECONDS:
            break
    return mean_embedding(vectors)


def resolve_targets(
    segments: list[dict],
    audio: np.ndarray,
    embedder: SpeakerEmbedder,
    enrollment: dict[str, np.ndarray],
    cfg: TranscribeConfig,
) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    """Costruisce i target di scoring per etichetta finale e la mappa raw->finale.

    Con enrollment: ogni etichetta enrollata viene agganciata al cluster piu' simile
    (identita' certa). Senza: mappa per ordine di comparsa e usa i centroidi.
    """
    raw_order = raw_speakers_in_order(segments)
    centroids = {r: build_cluster_centroid(segments, r, audio, embedder) for r in raw_order}
    centroids = {r: c for r, c in centroids.items() if c is not None}

    targets: dict[str, np.ndarray] = {}
    raw_to_final: dict[str, str] = {}
    used_raw: set[str] = set()

    # 1) Aggancia le etichette enrollate ai cluster piu' simili.
    for label in [l for l in cfg.speaker_labels if l in enrollment]:
        ref = enrollment[label]
        candidates = [(r, cosine(ref, centroids[r])) for r in centroids if r not in used_raw]
        if not candidates:
            targets[label] = ref
            continue
        best_raw = max(candidates, key=lambda x: x[1])[0]
        raw_to_final[best_raw] = label
        targets[label] = ref
        used_raw.add(best_raw)

    # 2) Cluster rimanenti -> etichette finali libere, per ordine di comparsa.
    free_labels = [l for l in cfg.speaker_labels if l not in targets]
    for raw in raw_order:
        if raw in used_raw:
            continue
        label = free_labels.pop(0) if free_labels else f"S{len(raw_to_final) + 1}"
        raw_to_final[raw] = label
        if raw in centroids:
            targets[label] = centroids[raw]
        used_raw.add(raw)

    return targets, raw_to_final


# -----------------------------------------------------------------------------
# Ri-assegnazione
# -----------------------------------------------------------------------------


def best_label(
    emb: np.ndarray, targets: dict[str, np.ndarray]
) -> tuple[Optional[str], float]:
    """Etichetta piu' simile e margine rispetto alla seconda."""
    sims = sorted(((cosine(emb, t), lbl) for lbl, t in targets.items()), reverse=True)
    if not sims:
        return None, 0.0
    if len(sims) == 1:
        return sims[0][1], 1.0
    return sims[0][1], sims[0][0] - sims[1][0]


def _set_speaker(seg: dict, label: str) -> None:
    seg["speaker"] = label
    for w in seg.get("words", []):
        if w.get("speaker") is not None:
            w["speaker"] = label


def split_segment(
    seg: dict,
    audio: np.ndarray,
    targets: dict[str, np.ndarray],
    embedder: SpeakerEmbedder,
    cfg: TranscribeConfig,
    fallback_label: str,
) -> list[dict]:
    """Spezza un segmento se contiene un inserto (>= finestra) dell'altro speaker.

    Assegna a ogni parola l'etichetta della finestra scorrevole che la copre; poi
    raggruppa le parole per etichetta, ignorando i run piu' brevi di refine_min_segment_s.
    """
    words = [w for w in seg.get("words", []) if w.get("start") is not None and w.get("end") is not None]
    dur = seg["end"] - seg["start"]
    if (
        not cfg.split_long_segments
        or len(words) < 3
        or dur < cfg.refine_min_segment_s + cfg.refine_window_s
    ):
        _set_speaker(seg, fallback_label)
        return [seg]

    # Etichette per finestra scorrevole.
    W = cfg.refine_window_s
    hop = max(0.25, W / 2)
    windows: list[tuple[float, float, Optional[str]]] = []
    t = seg["start"]
    while t < seg["end"]:
        w0, w1 = t, min(t + W, seg["end"])
        emb = embedder.embed(slice_audio(audio, w0, w1))
        lbl: Optional[str] = None
        if emb is not None:
            cand, margin = best_label(emb, targets)
            if margin >= cfg.refine_margin:
                lbl = cand
        windows.append((w0, w1, lbl))
        t += hop

    def label_for_word(w: dict) -> str:
        mid = (w["start"] + w["end"]) / 2
        for w0, w1, lbl in windows:
            if w0 <= mid < w1 and lbl is not None:
                return lbl
        return fallback_label

    labels = [label_for_word(w) for w in words]

    # Smoothing: run piu' corti di min_segment assorbiti dal vicino precedente.
    i = 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        run_dur = words[j - 1]["end"] - words[i]["start"]
        if run_dur < cfg.refine_min_segment_s and i > 0:
            for k in range(i, j):
                labels[k] = labels[i - 1]
        i = j

    # Raggruppa parole consecutive con stessa etichetta in sotto-segmenti.
    out: list[dict] = []
    i = 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        grp = words[i:j]
        out.append(
            {
                "start": grp[0]["start"],
                "end": grp[-1]["end"],
                "speaker": labels[i],
                "text": " ".join(w.get("word", "").strip() for w in grp).strip(),
                "words": [dict(w, speaker=labels[i]) for w in grp],
            }
        )
        i = j
    return out


def refine_segments(
    segments: list[dict],
    audio: np.ndarray,
    targets: dict[str, np.ndarray],
    raw_to_final: dict[str, str],
    embedder: SpeakerEmbedder,
    cfg: TranscribeConfig,
) -> tuple[list[dict], int]:
    refined: list[dict] = []
    reassigned = 0
    for seg in segments:
        start, end = seg.get("start"), seg.get("end")
        raw_final = raw_to_final.get(seg.get("speaker"), seg.get("speaker"))
        if start is None or end is None or (end - start) < cfg.refine_min_segment_s:
            _set_speaker(seg, raw_final)
            refined.append(seg)
            continue

        emb = embedder.embed(slice_audio(audio, start, end))
        chosen = raw_final
        if emb is not None:
            cand, margin = best_label(emb, targets)
            if cand is not None and margin >= cfg.refine_margin:
                chosen = cand

        parts = split_segment(seg, audio, targets, embedder, cfg, chosen)
        if chosen != raw_final or len(parts) > 1:
            reassigned += 1
        refined.extend(parts)
    return refined, reassigned


# -----------------------------------------------------------------------------
# Orchestrazione
# -----------------------------------------------------------------------------


def refine_one(json_path: Path, cfg: TranscribeConfig, embedder: SpeakerEmbedder) -> dict:
    data = read_json(json_path)
    stem = json_path.stem
    wav = cfg.stage0_dir / f"{stem}.wav"
    if not wav.is_file():
        raise SystemExit(f"WAV mancante per la rifinitura: {wav}")

    audio = load_audio_16k(wav)
    enrollment = build_enrollment_profiles(cfg, embedder)
    targets, raw_to_final = resolve_targets(data.get("segments", []), audio, embedder, enrollment, cfg)

    refined_segments, reassigned = refine_segments(
        data.get("segments", []), audio, targets, raw_to_final, embedder, cfg
    )

    data["segments"] = refined_segments
    data["refined"] = True
    data["refinement"] = {
        "embedding_model": cfg.embedding_model,
        "enrolled_labels": sorted(enrollment.keys()),
        "raw_to_final": raw_to_final,
        "segments_reassigned_or_split": reassigned,
        "min_segment_s": cfg.refine_min_segment_s,
        "margin": cfg.refine_margin,
        "split_long_segments": cfg.split_long_segments,
    }
    logger.info("[%s] segmenti rifiniti/spezzati: %d", stem, reassigned)
    return data


def run(cfg: TranscribeConfig, only_stem: str | None = None) -> list[dict]:
    if not cfg.refine_speakers:
        logger.info("Rifinitura disattivata (refine_speakers=false): stadio 1b saltato.")
        return []

    patch_speechbrain_compat()
    quiet_noisy_logs()
    configure_model_cache(cfg)

    if only_stem is not None:
        target = cfg.stage1_dir / f"{only_stem}.json"
        if not target.is_file():
            raise SystemExit(f"JSON atteso non trovato: {target}. Esegui prima lo stadio 1.")
        jsons = [target]
    else:
        jsons = sorted(
            p for p in cfg.stage1_dir.glob("*.json") if p.name != "transcribe_log.json"
        )
    if not jsons:
        raise SystemExit(f"Nessun JSON in {cfg.stage1_dir}. Esegui prima lo stadio 1.")

    out_dir = cfg.stage1b_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Caricamento modello di embedding: %s", cfg.embedding_model)
    embedder = SpeakerEmbedder(cfg.embedding_model, cfg.device, cfg.models_dir)

    started_at = iso_now()
    t0 = time.perf_counter()
    summary: list[dict] = []
    for p in jsons:
        data = refine_one(p, cfg, embedder)
        write_json(out_dir / p.name, data)
        summary.append(
            {"file": p.name, "reassigned": data["refinement"]["segments_reassigned_or_split"]}
        )
    elapsed = time.perf_counter() - t0

    write_json(
        out_dir / "refine_log.json",
        {
            "stage_version": STAGE_VERSION,
            "started_at": started_at,
            "finished_at": iso_now(),
            "elapsed_s": round(elapsed, 1),
            "embedding_model": cfg.embedding_model,
            "files": summary,
        },
    )
    print(
        f"\nStadio 1b completato in {format_duration(elapsed)}: "
        f"{len(summary)} file rifiniti -> {out_dir}"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stadio 1b: rifinitura speaker via embedding ECAPA.")
    parser.add_argument("--config", type=Path, default=None, help="Path a config.yaml")
    parser.add_argument("--only", type=str, default=None, help="Solo questo file (senza estensione).")
    args = parser.parse_args()
    run(load_config(args.config), args.only)


if __name__ == "__main__":
    main()
