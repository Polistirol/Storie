"""
Scarica l'audio da un video YouTube (nessun limite di durata lato script).

Dipendenze:
  pip install yt-dlp
  FFmpeg nel PATH: obbligatorio per mp3/wav; con -f native spesso non serve (solo download).

Uso:
  python tools/YT_downloader.py "https://www.youtube.com/watch?v=..." --format mp3
  python tools/YT_downloader.py "https://www.youtube.com/watch?v=pE9H2BzQNQU" -f native -o ./resources/audio_seed/adriano/adriano_full_yt.wav
  python tools/YT_downloader.py URL -f native -o ./out   # massima qualità: nessuna ricodifica (.m4a/.webm)
  python tools/YT_downloader.py URL -f mp3 --max-quality # MP3 a 320 kbps
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def download_audio(
    url: str,
    out_dir: Path,
    audio_format: str,
    *,
    mp3_quality: str = "192",
    max_quality: bool = False,
) -> Path:
    """
    Scarica il miglior flusso audio e, se richiesto, lo converte.

    Qualità:
    - ``native``: nessuna ricodifica (AAC/Opus/… come da YouTube) — fedeltà massima.
    - ``wav``: decodifica in PCM (una generazione; utile se ti serve .wav).
    - ``mp3``: lossy; con ``max_quality`` o ``mp3_quality=320`` usa bitrate alto.

    Returns:
        Path della directory di output (il nome file è generato da yt-dlp dal titolo/id).
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise SystemExit(
            "Manca il pacchetto yt-dlp. Installa con: pip install yt-dlp"
        ) from e

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    fmt = audio_format.lower().strip()
    if fmt in ("mpt", "mpeg3"):
        fmt = "mp3"
    if fmt not in ("mp3", "wav", "native"):
        raise ValueError("audio_format deve essere 'mp3', 'wav' o 'native'")

    if max_quality and fmt == "mp3":
        mp3_quality = "320"

    if fmt == "native":
        postprocessors: list[dict] = []
    else:
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
            }
        ]
        if fmt == "mp3":
            postprocessors[0]["preferredquality"] = mp3_quality

    ydl_opts: dict = {
        # Solo audio, miglior formato disponibile (file più “grande” = tipicamente migliore)
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "windowsfilenames": True,
        "noprogress": False,
        "postprocessors": postprocessors,
        # Evita segmenti / trim: scarica l'intero contenuto disponibile
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", url)
        logger.info("Completato: %s", title)

    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scarica audio da un link YouTube (mp3, wav o native; durata illimitata). "
            "Per la massima fedeltà al sorgente usa -f native."
        )
    )
    parser.add_argument("url", help="URL del video YouTube")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Cartella di destinazione (default: directory corrente)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("mp3", "wav", "mpt", "native"),
        default="mp3",
        help=(
            "mp3/wav/mpt oppure native = file originale senza ricodifica "
            "(.m4a/.webm, massima qualità possibile da YouTube)"
        ),
    )
    parser.add_argument(
        "--max-quality",
        action="store_true",
        help="MP3 a 320 kbps (solo con -f mp3/mpt)",
    )
    parser.add_argument(
        "--mp3-quality",
        default="192",
        help="Bitrate MP3 in kbps (solo per --format mp3; default: 192; usa --max-quality per 320)",
    )
    args = parser.parse_args()

    try:
        download_audio(
            args.url,
            args.output_dir,
            args.format,
            mp3_quality=args.mp3_quality,
            max_quality=args.max_quality,
        )
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Download fallito: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
