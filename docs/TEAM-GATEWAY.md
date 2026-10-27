# Gateway de ferramentas MCP (T07)

Entrega da issue [#22](https://github.com/rafaelmaiabr/assistente/issues/22): `team/gateway.py`, sobre acesso, conexões, agentes e skills ([TEAM-ACCESS.md](TEAM-ACCESS.md), [TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md), [TEAM-AGENTS.md](TEAM-AGENTS.md), [TEAM-SKILLS.md](TEAM-SKILLS.md)). É o único caminho previsto de uma execução de agente até uma ferramenta MCP.

> **Estado real:** o gateway está implementado e testado com um MCP **falso** e, desde a #102, também com um servidor MCP **real** de teste (stdio, HTTP e SSE) pelo adaptador `team/mcp_transport.py` (seção abaixo). O chat atual (`assistant_core.make_agent`, demo de email) ainda usa as ferramentas MCP direto, sem passar por ele — a integração é do runtime de agentes (T08). O adaptador ainda não está ligado ao `TeamApp`.

## Contrato do transporte

`McpTransport` tem dois métodos assíncronos: `describe(server, tool)` (metadados remotos, por exemplo `{"read_only": true}`) e `call_tool(server, tool, arguments, identity=…, secret=…)`. `server` é montado a partir da entrada aprovada do catálogo (transporte, URL, comando do launcher registrado). Há um transporte por tipo (`http`, `sse`, `stdio`).

## Regras de cada chamada

1. **Execução confiável.** `Gateway.open_execution(contexto_do_agente, run_id)` cria a `Execution` a partir de uma execução persistida e ativa. Quem não é o agente daquela execução, outra empresa ou objetos forjados são recusados. Identidade, conta e segredo são injetados pelo backend (`identity`, `secret`); o modelo só escolhe o nome da ferramenta e os argumentos.
2. **Listagem filtrada.** `list_tools` expõe só ferramentas que existem **ao mesmo tempo** em: concessão ativa do agente (na versão fixada da execução), skill ativa que declara a ferramenta, conexão ativa, entrada ativa do catálogo aprovado **com classificação** (`tool_specs`) e operação coberta pela concessão. Ferramenta aprovada sem classificação, ferramenta nova no servidor ou fora da concessão não aparece. Aliases são estáveis (`servidor.ferramenta`, `~2` se houver duas conexões); o modelo não escolhe conexão.
3. **Revalidação na execução.** `call_tool` recalcula as rotas, chama `SkillService.authorize_use` antes de executar e **de novo depois** de a ferramenta responder: concessão revogada, skill desativada, conexão inativa ou credencial revogada durante a execução descarta o resultado. Ferramenta oculta, revogada ou inexistente retorna o mesmo `ToolNotFound`.
4. **Classificação confiável.** A operação (`read`/`write`/`send`/`spend`) vem do catálogo aprovado pelo operador. Se o servidor remoto afirmar que uma ferramenta classificada como `read` não é somente leitura, a chamada é negada. Nomes de ferramenta genéricos de shell/execução (`shell`, `exec`, `eval`, `bash`, `powershell`, `run-command`…) não são aprovados no catálogo; não existe API de comando genérico.
5. **Argumentos.** Cada ferramenta tem regras no catálogo (`string`, `integer`, `number`, `boolean`, tamanho, mínimo/máximo, `enum`, obrigatório). Nomes desconhecidos são rejeitados e nomes de escopo/credencial (`tenant_id`, `connection_id`, `credential`, `token`, `account`…) são proibidos já na aprovação, então um modelo não consegue sobrescrever contexto.
6. **Limites e cancelamento.** Tempo (`timeout_seconds` da ferramenta, limitado pelo prazo da execução), número de chamadas por execução (padrão 20), bytes do resultado (padrão 64 KB) e itens (padrão 200), ajustáveis por ferramenta. `Execution.cancel()` interrompe a chamada em andamento e bloqueia as próximas. Erros tipados com mensagem fixa: `not_found`, `denied`, `invalid_arguments`, `timeout`, `cancelled`, `too_large`, `unavailable`, `budget_exceeded`, `blocked`.
7. **Resultados são dados.** Passam por serialização JSON, limite de tamanho e redação de segredos (chaves como `token`/`api_key`/`authorization` e valores com formato de chave); um resultado que repita a credencial é bloqueado pela camada do conector. `ToolResult.as_external_item()` entrega o dado ao prompt como conteúdo externo (escapado, fora do system).
8. **Registro mínimo.** Cada decisão grava em `tool_decisions` (migração `0005`): execução, agente, versão, nome da ferramenta, operação, `allowed`/`denied`/`error`, motivo, `policy_version` (`gateway-1`), bytes e duração — nunca argumentos nem resultados.

## Adaptador MCP real (#102)

`McpClientTransport(kind)` (`stdio`, `http` ou `sse`; um por tipo, em `Gateway(transports={...})`) usa o pacote `mcp` instalado; uma sessão por chamada (`describe` e `call_tool` conectam separadamente).

- **stdio:** executa o argv do *launcher* registrado pelo operador (caminho absoluto, sem shell), com ambiente limpo (padrões seguros da biblioteca + variáveis injetadas) e stderr descartado. Terminar a chamada (fim, tempo ou cancelamento) encerra o processo; há teste que confirma que não fica órfão.
- **http/sse:** TLS com certificado verificado (`ca_file` opcional para CA privada); `http://` só em loopback; URL com usuário/senha é recusada; redirecionamento para outra origem não é seguido (a biblioteca só segue dentro da mesma origem).
- **Identidade e segredo vêm do backend:** `Authorization: Bearer <segredo>` e `X-Assistente-Tenant-Id/Agent-Id/Run-Id/Connection-Id/Operation` por HTTP; `ASSISTENTE_SECRET` e `ASSISTENTE_*` por variáveis de ambiente em stdio. O modelo não define nenhum deles.
- **`describe`:** informa `read_only` só quando o servidor anota a ferramenta (`readOnlyHint`); sem anotação não há afirmação. Uma ferramenta aprovada como `read` que o servidor marca como não somente leitura é negada pelo gateway.
- **Falhas:** conexão, protocolo, queda do servidor ou erro do servidor viram `ToolUnavailable` ou a mensagem fixa `erro_do_servidor`; texto vindo do servidor nunca é copiado. Resultado acima de 1 MiB é recusado no adaptador (`ResultTooLarge`), antes do limite por ferramenta do gateway.

Validado com `tests/mcp_test_server.py` (servidor MCP real local) e com um certificado autoassinado gerado no teste. **Não** foi testado: servidor MCP de terceiros, MCP real sobre TLS aceito (só a rejeição de certificado inválido), autenticação OAuth do próprio servidor MCP e servidores com sessões de longa duração.

## Limites

Sem integração ao chat (T08); a chamada ainda não é paralelizada. Desde a T13, `write`/`send`/`spend` só executam com uma aprovação humana exata e de uso único (ver [TEAM-APPROVALS.md](TEAM-APPROVALS.md)); sem serviço de aprovações essas ferramentas ficam bloqueadas. O acesso ao SQLite é síncrono dentro do laço assíncrono (rápido em uso local; revisar em T34). A filtragem de listagem depende de o catálogo ter sido classificado pelo operador.
