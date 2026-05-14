# Crea ambiente base
micromamba create -n adriano_audio python=3.10 pip -c conda-forge -y

# Attiva
micromamba activate adriano_audio

# ffmpeg e libsndfile via conda
micromamba install ffmpeg libsndfile -c conda-forge -y

# Torch CUDA prima di tutto
pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128

# Trascrizione e allineamento
pip install stable-ts

# Audio, TTS e utility
pip install `
    pydub `
    sounddevice `
    numpy `
    omnivoice `
    python-dotenv

# Verifica CUDA
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"