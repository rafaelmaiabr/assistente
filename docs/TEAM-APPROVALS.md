# Aprovações humanas e controle do operador (T13)

Entrega da issue [#28](https://github.com/rafaelmaiabr/assistente/issues/28): `team/approvals.py` (`ApprovalService`), `team/control.py` (`OperatorControl`), integração no gateway (`team/gateway.py`) e migração `0011`. Sobre demandas, encaminhamentos, fila e gateway ([TEAM-CASES.md](TEAM-CASES.md), [TEAM-HANDOFFS.md](TEAM-HANDOFFS.md), [TEAM-QUEUE.md](TEAM-QUEUE.md), [TEAM-GATEWAY.md](TEAM-GATEWAY.md)).

## Uma aprovação = uma ação exata

`request` registra o que será mostrado ao operador: ação, empresa, demanda, recurso, destinatários (normalizados), valor em centavos e moeda, parâmetros, prazo, política (`aprovacao-1`) e quem pediu. Tudo isso forma o **payload normalizado**, cujo `payload_hash` (sha256 do JSON canônico) amarra a aprovação: mesma entrada, mesmo hash; trocar destinatário, valor, moeda, recurso, ação, demanda ou qualquer parâmetro muda o hash. Segredos e payloads grandes são recusados. Pedir duas vezes o mesmo payload devolve a solicitação aberta (índice único no banco).

Estados: `pendente`, `aprovada`, `negada`, `expirada`, `consumida`, `cancelada` (revogada). Enquanto há solicitação pendente a demanda fica em `aguardando_aprovacao` e volta a `em_execucao` quando não resta nenhuma.

**Padrão:** tudo que não é leitura exige aprovação (escrita externa, renovação, cobrança, pagamento, envio e qualquer ação desconhecida: fail closed). Não há autoaprovação nem regras de autonomia nesta entrega.

## Quem decide

Somente o **operador confiável** (o contexto de operador local): agentes, contextos derivados ou de outro operador recebem `PermissionDenied`. O operador confirma o que viu informando o hash exibido (`approve(…, shown_hash)`); hash diferente é recusado. Texto de email, mensagem de cliente, resultado de ferramenta ou resposta de LLM não tem caminho para a decisão: a decisão só existe como chamada autenticada ao serviço.

## Consumo (uso único)

`consume` é atômico e verifica: aprovação `aprovada`, payload recalculado idêntico (mesmo hash e mesma demanda), não expirada, demanda ativa (não encerrada, não pausada, sem controle do operador) e solicitante ainda responsável pela demanda. Em caso positivo marca `consumida`; **todas** as falhas levantam o mesmo `ApprovalInvalid`. Replay, duplo clique (4 threads: 1 vence), outra empresa, payload alterado, expiração, revogação, transferência da demanda e corrida aprovação/revogação bloqueiam a execução incorreta (nunca "consumida" e "revogada" ao mesmo tempo).

## Gateway: escrita só com aprovação exata

Ferramentas cuja operação não é `read` não executam sem aprovação: sem serviço de aprovações ou sem demanda associada a execução o gateway nega (`denied`). Com o serviço, a primeira chamada cria a solicitação (destinatário, valor e moeda vêm dos argumentos indicados em `recipient_argument`, `amount_argument` e `currency_argument` do catálogo) e responde `approval_required`; depois da aprovação, a mesma chamada consome a aprovação e executa; repetir a chamada pede nova aprovação. O resultado fica registrado na aprovação: `confirmado` (a ferramenta respondeu), `falhou` (a chamada nem saiu) ou **`desconhecido`** (saiu e a resposta não foi confirmada: timeout, cancelamento, concessão revogada durante a chamada). Efeitos desconhecidos aparecem em `needing_reconciliation`; o operador verifica o sistema externo e registra a verdade em `resolve_effect`. Não existe API para "desfazer" pagamento ou envio: correção é uma nova ação, também aprovada.

## Controle do operador (`OperatorControl`)

- `pause`: pausa a demanda (agentes não avançam).
- `take_over`: **assume** — cancela encaminhamentos abertos, revoga aprovações pendentes/aprovadas, pausa os jobs da demanda (modelo e chamadas MCP em andamento param no próximo heartbeat) e marca `operator_control`; agentes passam a receber `PermissionDenied` ao tentar transicionar, responder ao cliente ou pedir aprovação. O operador continua podendo agir.
- `reject`: cancela a demanda com motivo e retira tudo o que estava em andamento.
- `return_to_team`: devolve a demanda ao responsável (volta a `em_execucao` ou `triagem`) e libera os jobs.

Aprovações já consumidas e seus efeitos permanecem no histórico e na reconciliação.

## Limites

Sem interface de aprovação (T16): hoje a decisão é uma chamada ao serviço. O envio efetivo de email/cobrança e a renovação de domínio ainda não existem (T14, T22, T25–T27), então o mecanismo está validado com ferramentas MCP falsas. O operador local é um só (T32 traz autenticação e papéis). A aprovação garante que a execução corresponde ao que foi aprovado, não que o sistema externo se comporta de forma idempotente.
