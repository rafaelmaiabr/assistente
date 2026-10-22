$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Execute setup.ps1 antes de abrir o chat.' }
$ready = $false
try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
    $ready = $health.app -eq 'assistente' -and $health.version -eq 3
} catch {}
if ($health -and $health.app -eq 'assistente' -and $health.version -ne 3) {
    throw 'Servidor de uma versao anterior ativo. Execute encerrar-web.cmd e abra iniciar-web.cmd novamente.'
}
if (-not $ready) {
    $dist = Get-Item 'frontend\dist\index.html' -ErrorAction SilentlyContinue
    $sources = Get-ChildItem frontend/src,frontend/public,frontend/package.json,frontend/package-lock.json -Recurse -File
    if (-not $dist -or ($sources | Where-Object { $_.LastWriteTimeUtc -gt $dist.LastWriteTimeUtc })) {
        if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Instale Node.js 22 ou mais recente para compilar a interface.' }
        & npm.cmd ci --prefix frontend
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar a interface.' }
        & npm.cmd run build --prefix frontend
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao compilar a interface.' }
    }
    $rawConfig = & $python -c 'import json; from settings import load_config; print(json.dumps(load_config()))'
    if ($LASTEXITCODE -ne 0) { throw 'Configuracao invalida. Confira config.json.' }
    $config = $rawConfig | ConvertFrom-Json
    $ollamaUri = [Uri]$config.ollama_url
    $ollamaOrigin = $ollamaUri.GetLeftPart([System.UriPartial]::Authority)
    try { $null = Invoke-RestMethod "$ollamaOrigin/api/tags" -TimeoutSec 2 } catch {
        if ($ollamaUri.Host -notin @('localhost', '127.0.0.1')) { throw 'Inicie o provedor configurado antes de abrir a interface.' }
        $ollama = Get-Command ollama -ErrorAction Stop
        $env:OLLAMA_HOST = "127.0.0.1:$($ollamaUri.Port)"
        $existingModels = Join-Path $env:USERPROFILE '.ollama\models\manifests'
        if (Test-Path $existingModels) { $env:OLLAMA_MODELS = Join-Path $env:USERPROFILE '.ollama\models' }
        Remove-Item Env:OLLAMA_NO_GPU -ErrorAction SilentlyContinue
        Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden
    }
    $server = Start-Process -FilePath $python -ArgumentList ('"' + (Join-Path $PSScriptRoot 'web_server.py') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardError 'web-server-error.log' -RedirectStandardOutput 'web-server.log'
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($server.HasExited) { throw 'O servidor encerrou. Consulte web-server-error.log.' }
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
            if ($health.app -eq 'assistente' -and $health.version -eq 3) { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) {
        Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
        throw 'O servidor nao iniciou a tempo. Consulte web-server-error.log.'
    }
}
Start-Process 'http://127.0.0.1:8765'
Write-Host 'Chat aberto no navegador. Fechar a aba encerra a conversa; o servidor permanece disponivel.'
