$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv\Scripts\python.exe'
$listeners = Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)"
    if ($process.CommandLine -notmatch 'web_server\.py' -or ($process.ExecutablePath -ne $python -and $parent.ExecutablePath -ne $python)) {
        throw 'A porta pertence a outro processo. Nenhum processo foi encerrado.'
    }
    Stop-Process -Id $process.ProcessId -Force
}
Write-Host 'Servidor web encerrado. O Ollama permanece disponivel.'
