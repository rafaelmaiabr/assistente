# A2A: delegar a agentes externos (T35 e T40)

Issue [#50](https://github.com/rafaelmaiabr/assistente/issues/50), subdividida antes de codificar. **Parte 1 (#50):** o contrato e o adaptador isolado (`team/a2a.py`), testado com um par em memória. **Parte 2 ([#93 / T40](https://github.com/rafaelmaiabr/assistente/issues/93)):** as contrapartes autenticadas, as tarefas remotas persistidas e o retorno com timeout (`team/a2a_service.py`, migração 0024). Sem rede real e sem ligação ao aplicativo: o transporte HTTP é injetado (`transport_factory`) e ainda não existe um para agentes reais.

## ADR: o que ficou decidido

- **Versão: A2A 1.0.** Conferida na especificação oficial (a2a-protocol.org, versão 1.0.0; negociação por `A2A-Version: 1.0`). Versões anteriores (0.3, 0.2.x) não são aceitas: um cartão ou resposta de outra versão é recusado com `VersionNotSupportedError`.
- **Ligação (binding): JSON-RPC.** É a única que o adaptador declara. REST e gRPC ficam de fora até haver necessidade.
- **MCP continua sendo a fronteira de ferramentas.** A2A serve só para entregar uma tarefa a outro agente e ler a resposta; regras de dono, aprovação e idempotência pertencem aos serviços de domínio, não ao protocolo ([arquitetura](TEAM-ARCHITECTURE.md)).
- **Sem streaming e sem notificações push nesta parte.** O cartão publicado declara `streaming`, `pushNotifications` e `extendedAgentCard` como `false`; o acompanhamento é por `GetTask` (polling). Os callbacks (parte 2) são aceitos pelo serviço com token derivado, mas ainda não há listener HTTP nem envio da configuração de push ao par: o acompanhamento real é por polling.

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

## Contrapartes, tarefas remotas e retorno (parte 2)

- **Contraparte = conexão `a2a` por empresa.** Provedor fixo `a2a`, endereço `https` (`http` só em loopback) definido pelo operador, credencial no cofre (`set_credential`). Desativar a conexão ou revogar a credencial interrompe qualquer chamada seguinte na hora; o cartão do par nunca altera isso. Um agente precisa de concessão `write` na conexão para usá-la (o operador não precisa); a empresa B não enxerga nem usa a contraparte da A.
- **Tarefa remota = `a2a_task`** (tabela `a2a_tasks`, migração 0024, também em PostgreSQL): demanda, conexão, `messageId`, id remoto, estado local, resultado, prazo e sinal de atenção. `UNIQUE (empresa, messageId)` e uma tarefa remota por conexão: repetir não duplica.
- **A demanda continua com o dono.** Delegar só é permitido ao responsável atual (ou ao operador), a demanda passa a `aguardando_subtarefa` enquanto houver tarefa externa aberta (no máximo 3) e volta a `em_execucao` quando não houver mais nenhuma. O agente externo não vira dono nem ganha permissão de falar com o cliente.
- **Resultado é dado de fora.** Entra como nota interna marcada ("não é instrução nem evidência verificada"), redigido e limitado; nunca como evidência. Falha, recusa, cancelamento, expiração e pedido de decisão (`INPUT_REQUIRED`/`AUTH_REQUIRED`) deixam a demanda com o responsável e sinalizam o operador (`needing_attention`).
- **Resultado desconhecido não é sucesso.** A tarefa é gravada **antes** da chamada; se a resposta se perde (ou vem inválida) ela fica `desconhecido`, sinalizada, e não se repete às cegas. O operador pode `retry` (mesmo `messageId`, o par deduplica) ou `close_unknown` depois de verificar. Passado o prazo, uma tarefa desconhecida só é sinalizada.
- **Prazo e perda de conexão.** `expire_due` (gancho para o agendador) encerra aqui as tarefas abertas além do prazo, pede o cancelamento ao par em melhor esforço e sinaliza. Um polling sem resposta não muda o estado, só sinaliza.
- **Cancelar** pede ao par e vale o estado que ele devolve (se já tinha concluído, vale o concluído); sem resposta, fica cancelada aqui e marcada "não confirmada".
- **Callbacks.** `accept_callback` só aceita com o token derivado do segredo da conexão para aquela tarefa remota, e só de tarefa que esta empresa delegou por aquela conexão; qualquer recusa responde igual ("Notificação recusada"), sem dizer por quê. Repetição não muda nada e tarefa terminal não volta atrás.
- **Retenção:** apagar uma demanda apaga também as tarefas externas dela; o export da demanda as inclui.

## Validação

- `tests/test_team_a2a.py` (17), parte 1, com dois agentes de teste em memória (`tests/a2a_fake.py`): continuidade, tentativa repetida, estados interrompidos e incompatíveis, contexto ou tarefa trocados, resposta que não é tarefa, resultado limitado e redigido, versão e credencial recusadas, par fora do ar, saída sem segredos.
- `tests/test_team_a2a_service.py` (27), parte 2, nos dois bancos: delegação e dono, resultado como nota e não evidência, pedido duplicado, permissões (dono, operador, concessão), segredo no texto, isolamento entre empresas, limite por demanda, conexão (https, provedor), contraparte revogada ou desativada, credencial errada, resultado desconhecido (bloqueio, retry, encerramento, prazo), expiração, cancelamento (par que já concluiu, par fora do ar), repetição, contexto trocado, e callbacks válido, repetido, forjado (token, tarefa ou conexão desconhecidas), de outra empresa, de contraparte revogada e depois do encerramento.

## Limites (honestos)

- Nenhum agente A2A real foi contatado; não existe transporte HTTP real (o serviço recebe a fábrica de transporte) e o aplicativo ainda não usa o serviço: nada muda para quem usa o chat, a voz ou o MCP demo. Não há tela nem rota para as tarefas externas.
- O serviço não envia ao par a configuração de notificação push nem há listener para callbacks; hoje o acompanhamento é por polling.
- Os rótulos de campo seguem a especificação 1.0 lida em 2026-10-26: **não consegui ler** o texto completo de algumas seções (definição formal de `Role`/`Message` e regras de tarefa terminal); o literal `ROLE_USER` vem dos exemplos do documento. Conferir com o proto oficial antes de ligar a um par real.
- O prazo é por tarefa e só vale quando `expire_due` roda (nada o chama sozinho ainda).
