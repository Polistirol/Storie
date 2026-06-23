# Crea env adriano-kg (Adriano_graph + inference + visual/tools)
# Uso dalla root repo:
#   .\envs\install_kg.ps1

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

micromamba create -f environment.yml -y

Write-Host ""
Write-Host "Attiva:  micromamba activate adriano-kg"
Write-Host "Verifica:"
Write-Host '  python -c "import torch, fastapi, cv2, matplotlib; from sentence_transformers import SentenceTransformer; print(\"ok\", torch.__version__, torch.cuda.is_available())"'
