# Crea ambiente base
micromamba create -n adriano_dataset python=3.10 pip -c conda-forge -y

# Attiva
micromamba activate adriano_dataset

# Lettura epub
pip install `
    ebooklib `
    beautifulsoup4

# API Anthropic per generazione Q&A
pip install `
    anthropic

# Utility
pip install `
    python-dotenv

# Verifica
python -c "import ebooklib; import anthropic; print('dataset env ok')"