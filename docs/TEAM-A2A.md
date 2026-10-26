# A2A: delegar a agentes externos (T35, parte 1)

Parte 1 da issue [#50](https://github.com/rafaelmaiabr/assistente/issues/50), subdividida antes de codificar. Esta entrega é o **contrato e o adaptador isolado** (`team/a2a.py`), testado com um par em memória, **sem persistência, sem rede e sem ligação ao aplicativo**. As contrapartes autenticadas, as tarefas remotas persistidas e o retorno com timeout são a parte 2 ([#93 / T40](https://github.com/rafaelmaiabr/assistente/issues/93)); a #50 só fecha com ela.

## ADR: o que ficou decidido

- **Versão: A2A 1.0.** Conferida na especificação oficial (a2a-protocol.org, versão 1.0.0; negociação por `A2A-Version: 1.0`). Versões anteriores (0.3, 0.2.x) não são aceitas: um cartão ou resposta de outra versão é recusado com `VersionNotSupportedError`.
- **Ligação (binding): JSON-RPC.** É a única que o adaptador declara. REST e gRPC ficam de fora até haver necessidade.
- **MCP continua sendo a fronteira de ferramentas.** A2A serve só para entregar uma tarefa a outro agente e ler a resposta; regras de dono, aprovação e idempotência pertencem aos serviços de domínio, não ao protocolo ([arquitetura](TEAM-ARCHITECTURE.md)).
- **Sem streaming e sem notificações push nesta parte.** O cartão publicado declara `streaming`, `pushNotifications` e `extendedAgentCard` como `false`; o acompanhamento é por `GetTask` (polling). Callbacks (webhooks) entram na parte 2, onde ficam a autenticação de contraparte e a proteção contra callback forjado e SSRF.

## Mapeamento

| A2A 1.0 | Aqui |
|---|---|
| Task (`id`, `contextId`, `status.state`, `artifacts`) | uma subtarefa delegada; `contextId` é o id local opaco da troca (o mesmo em todas as chamadas) |
| Message (`messageId`, `role`, `parts`) | o envelope da delegação; `messageId` é estável entre tentativas, então repetir não cria uma segunda tarefa |
| Artifact | o resultado: só o texto, sem segredos (redigido) e limitado a 64 KB |
| `SUBMITTED` / `WORKING` | `pendente` / `em_execucao` |
| `INPUT_REQUIRED`, `AUTH_REQUIRED` | `aguardando` (uma pessoa decide; o agente externo não obtém credencial por isso) |
| `COMPLETED` | `concluida` |
| `FAILED`, `REJECTED` | `falhou` |
| `CANCELED` | `cancelada` |
| `CancelTask` | melhor esforço; vale o estado devolvido, não o pedido. Tarefa já terminal responde `TaskNotCancelableError` |

Mudanças de estado fora do permitido (por exemplo, uma tarefa terminal voltando a `WORKING`) são recusadas.

## O que o adaptador garante

- **O cartão descreve, não concede.** Um Agent Card remoto é validado (campos obrigatórios, interface JSON-RPC 1.0) e reduzido a poucas strings escapadas e curtas; `grants_access` é sempre `False` e esquemas de segurança, extensões e demais campos são ignorados. Cartão com segredo é recusado. Quem pode ser chamado, com qual credencial e para qual empresa é decisão do gateway (parte 2).
- **Saída pequena.** Só objetivo, resultado esperado, resumo e até 5 linhas de evidência escolhidas por quem delega; nunca o histórico da demanda, dados do cliente ou credenciais. Qualquer valor parecido com segredo recusa a mensagem inteira, antes de qualquer envio.
- **Entrada é dado não confiável.** A resposta precisa ser uma tarefa (exatamente uma de `task`/`message`), do contexto e da tarefa pedidos, com estado conhecido e transição permitida; o texto é redigido, limitado e nunca é instrução.
- **Transferir uma tarefa não transfere responsabilidade.** O dono do caso continua sendo o único a falar com o cliente; o agente externo não ganha essa permissão por aceitar a tarefa ([delegação](TEAM-HANDOFFS.md)).
- **Falha de rede é resultado desconhecido.** Timeout ou conexão perdida viram `A2AUnavailable`, nunca sucesso; só o tipo do erro aparece, nunca a URL ou a credencial.
- **Isolado do domínio.** `team/a2a.py` só importa utilitários de texto e de segredos (um teste confere); o transporte é uma interface (`A2ATransport`), como o `McpTransport` do gateway.

## Validação

`tests/test_team_a2a.py` (17), com dois agentes de teste em memória (`tests/a2a_fake.py`): continuidade (enviar, consultar, concluir, cancelar), tentativa repetida com o mesmo `messageId`, estados interrompidos, estados incompatíveis, contexto ou tarefa trocados, resposta que não é tarefa, resultado limitado e redigido, versão recusada, credencial recusada sem aparecer em mensagem, par fora do ar e erro de transporte, saída sem segredos.

## Limites (honestos)

- Nenhum agente A2A real foi contatado; o transporte HTTP real não existe ainda. Os rótulos de campo seguem a especificação 1.0 lida em 2026-10-26: **não consegui ler** o texto completo de algumas seções (definição formal de `Role`/`Message` e regras de tarefa terminal); o literal `ROLE_USER` vem dos exemplos do documento. Conferir com o proto oficial antes de ligar a um par real.
- Persistência, contrapartes, allowlist, revogação, tenant, callback e retorno com timeout **não existem** nesta parte (#93).
- O aplicativo não usa o adaptador ainda; nada muda para quem usa o chat, a voz ou o MCP demo.
