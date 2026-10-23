# Fila durável, workers e recuperação (T10)

Entrega da issue [#25](https://github.com/rafaelmaiabr/assistente/issues/25): `team/jobs.py` (fila), `team/worker.py` (workers, handler de execuções de agente e despachante da outbox), migração `0008`. Sobre persistência, demandas e runtime ([TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md), [TEAM-CASES.md](TEAM-CASES.md), [TEAM-RUNTIME.md](TEAM-RUNTIME.md)).

> **Estado real:** a fila e os workers existem e são testados, mas o aplicativo não inicia nenhum worker sozinho (o chat atual segue direto, sem fila). Iniciar workers junto do backend e do Electron, e a central que mostra os trabalhos, são entregas posteriores (T16/T29). `persistence.job_queue(root)` abre a fila do banco local.

## Jobs

Tabela `jobs` no mesmo SQLite: tipo, empresa, demanda e execução opcionais, `idempotency_key` (única por empresa), estado, prioridade, tentativas e máximo, `available_at`, lease (dono, validade, heartbeat), sinal de controle, `payload`, `checkpoint`, `result`, orçamento e último erro (código, sem texto livre). O relógio é injetável (`JobQueue(clock=…)`), então os testes não dormem.

Estados: `pendente`, `em_execucao`, `concluido`, `falhou`, `pausado`, `cancelado`, `esgotado`, `reconciliar`.

## Claim, lease e fencing

`claim(worker_id, kinds=…, lease_ms=…)` é atômico (`BEGIN IMMEDIATE`): recupera leases vencidos, escolhe o próximo job disponível por prioridade e ordem de criação, e o marca `em_execucao` com a tentativa incrementada. Cada ação do worker (`heartbeat`, `checkpoint`, `begin_effect`/`finish_effect`, `complete`, `fail`, `acknowledge`) exige o **token de fencing** (número da tentativa) e um lease válido; um worker que perdeu o lease não consegue mais alterar o job (`LeaseLost`), mesmo que volte com o mesmo nome. Vários workers (threads ou processos) nunca recebem o mesmo job.

**Uma execução ativa por demanda:** o banco tem índice único parcial (`em_execucao` por empresa+demanda), e o claim não escolhe job de uma demanda que já tem outro `em_execucao` ou `reconciliar`.

## Tentativas, backoff e recuperação

Falha com retry volta o job a `pendente` com backoff exponencial (1 s, 2 s, 4 s… teto de 5 min). Lease vencido (worker morreu) também devolve o job, com o backoff contado a partir do vencimento. Ao chegar a `max_attempts` o job fica `esgotado` e aparece em `needs_attention(empresa)` junto com `falhou` e `reconciliar`; só o operador o reabre (`retry_exhausted`). O `checkpoint` (até 64 KB) sobrevive à recuperação e ao reinício do processo.

## Efeitos externos e reconciliação

Antes de um efeito externo o handler chama `begin_effect(chave)` e, quando houver confirmação técnica, `finish_effect(chave, resultado)`.

- Efeito **confirmado** nunca se repete: numa nova tentativa `begin_effect` levanta `EffectAlreadyDone` com o resultado guardado.
- Efeito **iniciado e sem confirmação** (worker morreu no meio) leva o job a `reconciliar`: não é tentado de novo e bloqueia outros jobs da mesma demanda. O operador decide em `resolve_reconciliation`: `confirmado` (guarda o resultado), `nao_ocorreu` (libera nova tentativa) ou `abandonar` (marca `falhou`).

Não há "exactly-once" para sistemas externos: o contrato é at-least-once com idempotência e reconciliação. Hoje nenhum efeito de escrita externa é executado (aprovações: T13; envio: T14), então isto vale como mecanismo pronto para eles.

## Pausa, cancelamento e orçamento

`pause`, `resume` e `cancel` atuam no job e em todos os descendentes (`parent_job_id`). Jobs parados mudam de estado na hora; o que está rodando recebe o sinal no próximo heartbeat e o worker o encerra (`acknowledge`); se o worker morrer, o sinal vale quando o lease vencer. Para execuções de agente o sinal cancela a chamada ao modelo e a chamada de ferramenta em andamento (via `runtime.cancel`, que propaga ao gateway/MCP). Pausar uma execução cancela a que está em curso; ao retomar, o handler cria uma nova execução da **mesma versão** do agente. Um filho só pode reservar o que o pai ainda tem de passos e tokens e não pode passar do prazo do pai (`BudgetError`).

## Mensagem duplicada e outbox

`enqueue_command` registra o comando (chave de idempotência) e cria o job na mesma operação lógica: repetir a mensagem não cria outro job. `enqueue` com a mesma chave devolve o job existente.

`OutboxDispatcher` entrega eventos confirmados aos assinantes **ao menos uma vez e em ordem por empresa**: só marca a linha como despachada depois de todos os assinantes responderem; um travamento no meio reentrega após o reinício. Falha repetida bloqueia a fila (preserva a ordem) até `max_attempts`, quando a linha vira dead letter visível (`dead_letters`) e é pulada. Consumidores deduplicam com `JobQueue.consume_once(consumidor, id_do_evento)`.

## Contrato para um worker remoto futuro

Mesmas operações sobre um canal autenticado (T32/T34): `claim` → job com `token`; `heartbeat(job, worker, token)` retorna o sinal de controle; `checkpoint`; `begin_effect`/`finish_effect`; `complete`/`fail`/`acknowledge`. Todas validam dono, token e validade do lease; identidade do worker e filas por empresa/conta/modelo (quotas) ficam para a T34. Não está implementado como serviço de rede.

## Limites

Sem worker iniciado automaticamente nem interface; a fila e o runtime ainda não são acionados pelo chat. `agent_run_handler` presume a semântica do runtime atual (uma execução = uma chamada de `run`); reexecutar uma execução que ficou `em_execucao` repete as chamadas ao modelo. Ordem dos jobs é FIFO por prioridade; justiça e quotas por empresa são T34. Validado com banco temporário, relógio falso, dois workers em threads e asyncio; não houve teste com processos separados nem de carga.
