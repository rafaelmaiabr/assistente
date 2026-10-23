# Delegação, transferência e retorno entre agentes (T11)

Entrega da issue [#26](https://github.com/rafaelmaiabr/assistente/issues/26): `team/handoffs.py` (`HandoffService`), com ajustes em `team/cases.py` (responsável único) e migração `0009`. Sobre demandas, acesso e agentes ([TEAM-CASES.md](TEAM-CASES.md), [TEAM-ACCESS.md](TEAM-ACCESS.md), [TEAM-AGENTS.md](TEAM-AGENTS.md)). A Secretaria que decide *quando* encaminhar é a T12; aqui está o mecanismo.

## Duas operações diferentes

- **Delegação** (`delegate`): cria uma **subtarefa** para outro agente. A demanda **continua com o dono** e só ele fala com o cliente. O dono passa a `aguardando_subtarefa` e volta a `em_execucao` quando não restar subtarefa aberta. O destinatário aceita, executa e devolve (`complete_task`) ou falha (`fail_task`).
- **Transferência** (`propose_transfer`/`accept`): **proposta + aceite**. Até o aceite o responsável é o anterior. O aceite troca o dono na mesma transação, **somente se a demanda não mudou** desde a proposta (revisão esperada); se mudou, a proposta é cancelada, o operador é avisado e é preciso propor de novo. Só pode haver uma transferência pendente por demanda (índice único no banco).

Depois de transferida, o dono anterior perde o direito de agir: agentes só fazem transições e mensagens de saída enquanto forem o responsável atual (`PermissionDenied`). A comunicação externa propriamente dita é da T14.

## Envelope

Motivo, objetivo, resultado esperado, prazo (`due_at` da subtarefa e validade do aceite, padrão 1 h), resumo mínimo (≤ 1000 caracteres), ids de evidências *da mesma demanda* e `return_to` opcional (padrão: quem delegou). Textos passam pela remoção de raciocínio do modelo e pelo contrato (segredos embutidos são recusados). O destinatário lê apenas o envelope e as evidências citadas (`view`), nunca a linha do tempo, as notas internas ou as demais evidências.

## Nenhuma elevação de acesso

- O destino precisa estar na lista `handoff_target_ids` do agente que encaminha (o operador pode abrir exceção explícita).
- Aceitar uma transferência **não concede acesso**: o novo dono precisa ter por si só uma concessão de escrita na demanda; senão o aceite é recusado e a proposta continua pendente.
- Um assignee de subtarefa não vê a demanda: só a subtarefa e o envelope. Evidências que ele registra seguem as mesmas regras de qualquer agente (verificada exige tipo técnico e hash).
- Agentes só enxergam encaminhamentos dos quais participam; outra empresa recebe `NotFound`.

## Falha, recusa e prazo

Recusa (`decline`), expiração (`expire_due`, varredura do backend/agendador) e cancelamento mantêm a demanda com o dono anterior, cancelam a subtarefa pendente, registram nota interna e evento, e marcam o encaminhamento com `attention` para o operador (`needing_attention`/`resolve_attention`). Aceitar depois do prazo também é recusado e registrado como expirado. Falha da subtarefa devolve o problema ao dono e ao operador.

## Limites contra laços

Máximo de 5 transferências aceitas por demanda, profundidade de delegação 3, 5 subtarefas abertas, sem encaminhar para si mesmo, **sem ciclos** (delegar a quem já participa da cadeia: dono ou ancestrais; transferir a quem já foi responsável). Estourar um limite pede intervenção do operador.

## Registro

Cada proposta e decisão altera a revisão da demanda e grava evento e outbox na mesma transação (`handoff.proposed|accepted|declined|expired|cancelled|failed`, `case.transferred`, `case.subtask-returned`). A chave de idempotência evita propostas duplicadas.

## Limites desta entrega

Sem interface (T16) nem triagem automática (T12); o prazo é verificado por `expire_due`, que precisa ser chamado por um agendador (T29); sem aprovações humanas (T13). Validado com agentes de teste e LLM falso: Secretaria→Suporte→Financeiro→Suporte, Secretaria→Financeiro direto, aceitação concorrente (threads), timeout, ciclos e limites. Não há teste com agentes reais respondendo a encaminhamentos.
