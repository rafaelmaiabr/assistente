<#
.SYNOPSIS
  Sobe (ou derruba) um PostgreSQL local em Docker para a equipe de agentes.

.EXAMPLE
  .\deploy\postgres\dev.ps1            # banco persistente em volume; cria a senha na primeira vez
  .\deploy\postgres\dev.ps1 -Test      # banco DESCARTAVEL para os testes (porta 55432, apagado ao parar)
  .\deploy\postgres\dev.ps1 -Down      # para o banco persistente (os dados ficam no volume)

  A senha do banco persistente fica em deploy\postgres\secrets\pg_password.txt (ignorado pelo Git). Para o aplicativo, guarde a
  conexao no cofre ou em uma variavel de ambiente; este script nunca imprime a senha do banco persistente.
#>
param([switch]$Test, [switch]$Down, [int]$Port = 5432)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
function New-Password { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 28 | ForEach-Object { [char]$_ }) }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker nao encontrado. Instale o Docker Desktop e abra-o.' }
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'O Docker esta instalado mas parado. Abra o Docker Desktop e tente de novo.' }

if ($Test) {
  $password = New-Password
  docker rm -f assistente-pg-test *> $null
  docker run -d --rm --name assistente-pg-test -e POSTGRES_USER=assistente -e POSTGRES_DB=assistente_test -e "POSTGRES_PASSWORD=$password" -p 127.0.0.1:55432:5432 postgres:17-alpine | Out-Null
  for ($i = 0; $i -lt 40; $i++) { docker exec assistente-pg-test pg_isready -U assistente -d assistente_test *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep -Milliseconds 500 }
  Write-Host 'Banco de teste descartavel pronto. Rode, nesta janela, antes dos testes:'
  Write-Host "  `$env:ASSISTENTE_TEST_POSTGRES_DSN = 'postgresql://assistente:$password@127.0.0.1:55432/assistente_test'"
  Write-Host 'Para apagar: docker stop assistente-pg-test'
  return
}
$env:ASSISTENTE_PG_PORT = "$Port"
if ($Down) { docker compose -f "$here\compose.yaml" down; return }
$secrets = Join-Path $here 'secrets'
New-Item -ItemType Directory -Force $secrets | Out-Null
$file = Join-Path $secrets 'pg_password.txt'
if (-not (Test-Path $file)) { [IO.File]::WriteAllText($file, (New-Password)); Write-Host "Senha criada em $file (nao a publique)." }
docker compose -f "$here\compose.yaml" up -d --wait
Write-Host "PostgreSQL pronto em 127.0.0.1:$Port (banco 'assistente', usuario 'assistente'). Guarde a conexao no cofre do aplicativo ou em uma variavel de ambiente."
