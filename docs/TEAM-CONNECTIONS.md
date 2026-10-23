# Conexões, credenciais e catálogo MCP (T04)

Entrega da issue [#19](https://github.com/rafaelmaiabr/assistente/issues/19). Serviço interno em `team/connections.py` e `team/vault.py`, sobre contratos, persistência e acesso ([TEAM-CONTRACTS.md](TEAM-CONTRACTS.md), [TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md), [TEAM-ACCESS.md](TEAM-ACCESS.md)). **Não há tela nem endpoint HTTP** para isso ainda (painel de conexões: T15); o serviço retorna só dicionários de estado.

## O que um documento contém

`connection`: nome, provedor, tipo (`mcp`, `imap`, `smtp`, `http`, `outro`), transporte/origem (MCP vêm do catálogo), `scopes`, status e **`credential_ref`**, que aponta para um registro `credential` (estado `ativa`/`revogada`, onde está guardada). O valor do segredo nunca entra em documento, banco, log, resposta ou mensagem de erro; o contrato ainda rejeita segredos embutidos. Gatilhos no SQLite impedem que uma conexão aponte para credencial de outra empresa.

## Cofre

`SecretVault` (`put/get/delete` por `(tenant_id, credential_id)`):

- `WindowsVault`: Gerenciador de Credenciais do Windows, entradas `Assistente/<escopo da instalação>/team/<empresa>/<credencial>`. Não há variável de ambiente nem arquivo global com segredos; cada empresa só alcança as suas.
- `MemoryVault`: cofre falso usado na CI.
- Teste local descartável (pede pywin32): `.venv/Scripts/python.exe scripts/smoke_vault.py` grava um segredo aleatório em escopo temporário, lê, substitui, confirma isolamento entre empresas e remove; não imprime o segredo nem deixa entradas.

## Ciclo da credencial (somente operador)

| Operação | Efeito |
|---|---|
| `set_credential` | Grava no cofre e registra metadados; se já existe, substitui o segredo (e reativa uma revogada). Falha do cofre ou do banco não deixa segredo órfão nem registro pela metade. |
| `revoke_credential` | Marca `revogada` e apaga o valor do cofre; o registro permanece e nenhum uso é permitido. |
| `remove_credential` | Apaga o valor, remove o registro e desvincula da conexão. |
| `credential_status` | Estado e `available`; nunca o valor. |

Agentes não gerenciam credenciais. Erros usam textos fixos ("Cofre de credenciais indisponivel.", etc.).

## Uso: só dentro do conector autorizado

`use_credential(ctx, connection_id, conector, fn)` lê o segredo e chama `fn(segredo)`. Exige: conexão visível ao contexto (agente só com concessão de leitura), conexão ativa, credencial ativa e que o nome do conector esteja registrado pelo backend (`register_connector`) e seja o provedor da conexão. Exceções do conector viram `ConnectorFailed` sem detalhes; resposta que contenha o segredo é bloqueada. O segredo não é devolvido a quem chamou.

## Catálogo de servidores MCP aprovados

Entradas globais aprovadas pelo operador: chave, versão, transporte, origem (`url` para http/sse), ferramentas declaradas e status. Conexões `mcp` só podem apontar para entrada **ativa** e herdam transporte e origem dela (não aceitam endpoint informado à parte).

**stdio:** a entrada guarda o nome de um *launcher* (`launcher`), nunca uma linha de comando. Os launchers são cadastrados só pelo operador (`LauncherRegistry.register`, arquivo `team-data/launchers.json`, caminho absoluto + argumentos como lista, sem shell). Texto sugerido por LLM, mensagem ou ferramenta não consegue criar nem alterar launcher. O runtime obtém o comando por `stdio_command`, que devolve `None` se a entrada estiver inativa. A aplicação das ferramentas declaradas é do gateway (T07).

## Configuração antiga

- `config.json` e `mcp_servers` continuam funcionando como antes; nada é alterado.
- `import_legacy_mcp` registra cada URL antiga como entrada **inativa** (`legado-N`), aguardando revisão do operador: default-deny.
- **ElevenLabs:** `adopt_elevenlabs` cria uma conexão `elevenlabs` que *aponta* para a chave já salva pelas configurações de voz (armazenamento `elevenlabs-legado`). Nada é copiado ou movido, os perfis de voz seguem lendo a mesma entrada e a substituição continua sendo feita nas configurações de voz. Revogar/remover pelo serviço não apaga a chave da voz.

## Limites

Sem interface/HTTP (T15), sem OAuth (T19/T20) e sem rotação automática. O cofre do Windows protege o segredo de outros usuários, não de processos da mesma conta (ver limites em TEAM-ACCESS.md). Conectores reais (IMAP, SMTP, registrador) ainda não existem; aqui só o mecanismo de resolução. Teste real do cofre só foi feito localmente com o script acima, não na CI.
