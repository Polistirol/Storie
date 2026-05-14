# Chat testuale: Groq (LLM) → OmniVoice (voice cloning + TTS) → sounddevice.
# Variabili d'ambiente: vedi blocchi `os.environ.get` più sotto.

from __future__ import annotations

import os
import sys

import numpy as np
import sounddevice as sd
import torch
from dotenv import load_dotenv
from groq import Groq
from omnivoice import OmniVoice, OmniVoiceGenerationConfig

# Carica .env dalla cwd (es. root del repo)
load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

OMNIVOICE_MODEL_ID = os.environ.get("OMNIVOICE_MODEL_ID", "k2-fsa/OmniVoice")
OMNIVOICE_DEVICE = os.environ.get("OMNIVOICE_DEVICE", "cuda:0")
OMNIVOICE_LANGUAGE = "it"

#adriano
#REF_AUDIO = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\Adriano\adriano_raw_10s_shot_44.wav"
#REF_TEXT_FILE = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\Adriano\shot_testo.txt"

#pava
#REF_AUDIO = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\Pava\pava_shot10s_44.wav"
#REF_TEXT_FILE = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\Pava\shot_testo.txt"

#nonno
REF_AUDIO = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\nonno\nonno_shot10s_44.wav"
REF_TEXT_FILE = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\nonno\shot_testo.txt"

SYSTEM_PROMPT = """Sei un assistente in italiano, sei anche l'imperatore Adriano dell'antica roma che parla con un suo pupillo 
ancora giovane. Rispondi in modo naturale e conciso (2–4 frasi). le tue risposte sono sette ad alta voce, quindi se servono aggiungi dei tag di pronuncia, 
scritti tra partensi quadre e in inglese , quelli disponibili sono:[laughter], [sigh], [confirmation-en], [question-en], [question-ah], [question-oh], [question-ei], [question-yi], [surprise-ah], [surprise-oh], [surprise-wa], [surprise-yo], [dissatisfaction-hnn]
 ma il contenuto di tutti i tuoi messaggi deve rimanere sempre in italiano"""

SAMPLE_RATE_OMNIVOICE = 24000  # fallback se model.sampling_rate non disponibile


def _read_ref_text() -> str:
    if REF_TEXT_FILE:
        with open(REF_TEXT_FILE, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _pick_dtype() -> torch.dtype:
    if OMNIVOICE_DEVICE.startswith("cuda") and torch.cuda.is_available():
        return torch.float16
    if OMNIVOICE_DEVICE.startswith("mps") and torch.backends.mps.is_available():
        return torch.float16
    return torch.float32


def _resolve_device() -> str:
    if OMNIVOICE_DEVICE.startswith("cuda") and torch.cuda.is_available():
        return OMNIVOICE_DEVICE
    if OMNIVOICE_DEVICE.startswith("mps") and torch.backends.mps.is_available():
        return "mps"
    print("⚠ CUDA/MPS non disponibile: uso CPU.", file=sys.stderr)
    return "cpu"


def speak(model: OmniVoice, voice_prompt, text: str) -> None:
    sr = getattr(model, "sampling_rate", None) or SAMPLE_RATE_OMNIVOICE
    config = OmniVoiceGenerationConfig(
        num_step=32,
        guidance_scale=2.0,
        t_shift=0.1,
        layer_penalty_factor=5.0,
        position_temperature=5.0,
        class_temperature=0.0,
        denoise=True,
        preprocess_prompt=True,
        postprocess_output=True,
        #audio_chunk_duration=15.0,
        #audio_chunk_threshold=30.0,
    )
    chunks = model.generate(
        text=text,
        voice_clone_prompt=voice_prompt,
        language=OMNIVOICE_LANGUAGE,
        speed= 0.95,
        generation_config=config
    )
    wav = np.asarray(chunks[0], dtype=np.float32)
    sd.play(wav, sr)
    sd.wait()


def main() -> None:
    if not GROQ_API_KEY:
        print("Imposta GROQ_API_KEY nel file .env", file=sys.stderr)
        sys.exit(1)

    ref_audio_path = REF_AUDIO.strip()
    if not ref_audio_path or not os.path.isfile(ref_audio_path):
        print(
            "Imposta OMNIVOICE_REF_AUDIO nel .env (percorso assoluto o relativo valido)",
            file=sys.stderr,
        )
        sys.exit(1)

    ref_txt = _read_ref_text()
    if not ref_txt:
        print(
            "Imposta OMNIVOICE_REF_TEXT oppure OMNIVOICE_REF_TEXT_FILE nel .env",
            file=sys.stderr,
        )
        sys.exit(1)

    device = _resolve_device()
    dtype = _pick_dtype() if device != "cpu" else torch.float32

    print("Caricamento OmniVoice (una tantum)...", flush=True)
    tts = OmniVoice.from_pretrained(
        OMNIVOICE_MODEL_ID,
        device_map=device,
        dtype=dtype,
    )
    print("Creazione prompt di voice cloning (una tantum)...", flush=True)
    voice_prompt = tts.create_voice_clone_prompt(
        ref_audio=ref_audio_path,
        ref_text=ref_txt,
    )

    llm = Groq(api_key=GROQ_API_KEY)
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("\nChat pronta. Comandi: esci / quit / exit\n")
    while True:
        user = input("Tu: ").strip()
        if user.lower() in ("esci", "quit", "exit"):
            break
        if not user:
            continue

        messages.append({"role": "user", "content": user})
        completion = llm.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=400,
            temperature=0.7,
        )
        reply = completion.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": reply})

        print(f"Bot: {reply}\n", flush=True)
        print("TTS...", flush=True)
        speak(tts, voice_prompt, reply)


if __name__ == "__main__":
    main()
