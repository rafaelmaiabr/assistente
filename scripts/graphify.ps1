param(
    [switch]$Install,
    [ValidateSet('extract', 'query', 'explain', 'path')][string]$Action = 'extract',
    [string]$Query,
    [string]$Target
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
$previousRefresh = $env:GRAPHIFY_NO_AUTO_REFRESH
try {
    $env:GRAPHIFY_NO_AUTO_REFRESH = '1'
    if ($Install) {
        if (-not (Test-Path '.graphify-venv\Scripts\python.exe')) {
            & uv venv --python 3.13 .graphify-venv
            if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar ambiente Graphify.' }
        }
        & uv pip install --python .graphify-venv/Scripts/python.exe -r requirements-graphify.txt
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar Graphify.' }
    }
    $cli = Join-Path $root '.graphify-venv\Scripts\graphify.exe'
    if (-not (Test-Path $cli)) { throw 'Execute scripts\graphify.ps1 -Install primeiro.' }
    switch ($Action) {
        'extract' { & $cli extract . --code-only --no-cluster }
        'query' {
            if (-not $Query) { throw 'Informe -Query.' }
            & $cli query $Query
        }
        'explain' {
            if (-not $Query) { throw 'Informe -Query com o nome de um simbolo.' }
            & $cli explain $Query
        }
        'path' {
            if (-not $Query -or -not $Target) { throw 'Informe -Query e -Target.' }
            & $cli path $Query $Target
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "Graphify retornou codigo $LASTEXITCODE. Consulte a mensagem acima." }
} finally {
    $env:GRAPHIFY_NO_AUTO_REFRESH = $previousRefresh
    Pop-Location
}
