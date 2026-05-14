# chatbot_voce.py
# Dipendenze: pip install groq openai sounddevice soundfile numpy

import groq
import sounddevice as sd
import soundfile as sf
import numpy as np
import tempfile
import os
from openai import OpenAI

# ── Configurazione ──────────────────────────────────────────
GROQ_API_KEY = "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"
QWEN_BASE_URL = "http://127.0.0.1:8188"  # aggiusta con il tuo endpoint ComfyUI/Qwen
VOICE_CLIP = "tuo_clip_riferimento.wav"  # il tuo clip da 10-20 sec per zero-shot

SYSTEM_PROMPT = """Sei un assistente conversazionale in italiano.
Rispondi in modo naturale e conciso. Massimo 2-3 frasi per risposta.
Rispondi SEMPRE in italiano."""

# ── LLM via Groq ────────────────────────────────────────────
groq_client = groq.Groq(api_key=GROQ_API_KEY)
conversazione = []

def chiedi_llm(testo_utente):
    conversazione.append({"role": "user", "content": testo_utente})
    risposta = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",  # ottimo free tier, veloce
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversazione,
        max_tokens=200,
        temperature=0.7
    )
    testo = risposta.choices[0].message.content
    conversazione.append({"role": "assistant", "content": testo})
    return testo

# ── TTS via Qwen3 ───────────────────────────────────────────
def genera_audio(testo, stile="natural and conversational"):
    # Adatta questa funzione al tuo setup Qwen3 in ComfyUI
    # Placeholder — sostituisci con la tua chiamata reale
    pass

# ── Riproduzione audio ──────────────────────────────────────
def riproduci(path_audio):
    data, samplerate = sf.read(path_audio)
    sd.play(data, samplerate)
    sd.wait()

# ── Loop principale ─────────────────────────────────────────
def main():
    print("Chatbot pronto. Scrivi 'esci' per uscire.\n")
    while True:
        testo = input("Tu: ").strip()
        if testo.lower() in ["esci", "exit", "quit"]:
            break
        if not testo:
            continue

        print("Bot: ", end="", flush=True)
        risposta = chiedi_llm(testo)
        print(risposta)

        # audio_path = genera_audio(risposta)
        # if audio_path:
            # riproduci(audio_path)

if __name__ == "__main__":
    main()