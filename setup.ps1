param([switch]$Graphify)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Instale uv antes de continuar: https://docs.astral.sh/uv/getting-started/installation/'
}
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & uv venv --python 3.13 .venv
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar ambiente Python.' }
}
& uv pip install --python .venv/Scripts/python.exe -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependencias.' }
if (-not (Test-Path config.json)) { Copy-Item config.example.json config.json }
if ($Graphify) {
    & (Join-Path $PSScriptRoot 'scripts\graphify.ps1') -Install
}
Write-Host 'Pronto. Configure Ollama/modelo em config.json e abra iniciar.cmd.'
