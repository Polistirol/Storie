from omnivoice import OmniVoice
import soundfile as sf
import torch


toRead = """Sentinella! Costa cinque , 
ti da più una carta, più un azione... 
poi guardi le prime due carte dal mazzo e decidi quali eliminare. 
quali scartare. e quali rimettere a posto...
quindi ti pulisci il mazzo e ti sistemi le carte. fortissima!"""

ref_audio = r"C:\Users\Pc-Gaming\Documents\Repositories\Storie\resources\audio_seed\Pava\pava_shot10s_44.wav"
ref_text = """Niente, cioè secondo me si potrebbe fare qua 
tranquillamente, non è un problema. l'unica cosa è capire quanti sono. 
se siamo fino a otto, secondo me, stiamo anche comodi."""

print("Loading model..")
model = OmniVoice.from_pretrained(
    "k2-fsa/OmniVoice",
    device_map="cuda:0",
    dtype=torch.float16
)

print("model ok")
# Apple Silicon users: use device_map="mps" instead

#audio = model.generate(
#    text="Hello, this is a test of zero-shot voice cloning.",
#    ref_audio="ref.wav",
#    ref_text="Transcription of the reference audio.",
#) # audio is a list of `np.ndarray` with shape (T,) at 24 kHz.
#
print("generating..")
audio = model.generate(
    text=toRead,
    ref_audio=ref_audio,
    ref_text=ref_text,
    language_id= "it"
) # audio is a list of `np.ndarray` with shape (T,) at 24 kHz.

# If you don't want to input `ref_text` manually, you can directly omit the `ref_text`.
# The model will use Whisper ASR to auto-transcribe it.
print("saving")
sf.write("out.wav", audio[0], 24000)
print("done")
