param([switch]$Texto)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) { throw 'Execute setup.ps1 primeiro.' }
if (-not (Test-Path config.json)) { Copy-Item config.example.json config.json }
$env:OLLAMA_MODELS = Join-Path $env:USERPROFILE '.ollama\models'
$env:OLLAMA_HOST = '127.0.0.1:11435'
Remove-Item Env:OLLAMA_NO_GPU -ErrorAction SilentlyContinue
try { $null = Invoke-RestMethod 'http://127.0.0.1:11435/api/tags' } catch {
    Start-Process -FilePath (Get-Command ollama).Source -ArgumentList 'serve' -WindowStyle Hidden
    Start-Sleep -Seconds 3
}
$config = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$consoleArgs = @('agent.py', 'console', '--log-level', 'info')
if ($config.input_device) { $consoleArgs += @('--input-device', [string]$config.input_device) }
if ($config.output_device) { $consoleArgs += @('--output-device', [string]$config.output_device) }
if ($Texto) { $consoleArgs += '--text' }
Write-Host "Microfone: $($config.input_device)"
Write-Host "Saida: $($config.output_device)"
Write-Host 'Aguarde a inicializacao. Ctrl+T alterna voz/texto; Ctrl+C encerra.'
& .\.venv\Scripts\python.exe @consoleArgs
