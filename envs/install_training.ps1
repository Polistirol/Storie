# Crea ambiente base
micromamba create -n adriano_training python=3.10 pip -c conda-forge -y

# Attiva
micromamba activate adriano_training

# Torch CUDA prima di tutto
pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128

# Fine-tuning stack
pip install `
    transformers `
    trl `
    peft `
    datasets `
    accelerate `
    bitsandbytes `
    python-dotenv

# Verifica
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
