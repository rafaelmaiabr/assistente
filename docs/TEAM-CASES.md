# Demandas persistentes, evidências e máquina de estados (T09)

Entrega da issue [#24](https://github.com/rafaelmaiabr/assistente/issues/24): `team/cases.py`, sobre contratos, persistência e acesso ([TEAM-CONTRACTS.md](TEAM-CONTRACTS.md), [TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md), [TEAM-ACCESS.md](TEAM-ACCESS.md)). Ainda não há tela nem HTTP (central de demandas: T16); delegação e transferência vêm em T11.

## A demanda

Campos: empresa, departamento, cliente (`customer_ref`), objetivo, estado, **responsável** (`owner_agent_id`), revisão, prioridade (`baixa`/`normal`/`alta`/`urgente`) e origem (`operador`/`chat`/`email`/`agendamento`/`agente`). Evidências e entradas do histórico ficam em tabelas próprias ligadas à demanda.

**Um responsável ou a fila inicial.** Em `recebida` e `triagem` (e em `cancelada`/`falhou` sem trabalho) a demanda está na fila inicial, sem dono. Depois disso o contrato exige exatamente um `owner_agent_id`. `assign_owner` define o dono uma vez, e só na fila inicial, com agente ativo da mesma empresa; mudar de dono depois é transferência (T11), não uma edição.

## Estados e transições

Estados: `recebida`, `triagem`, `em_execucao`, `aguardando_cliente`, `aguardando_aprovacao`, `aguardando_subtarefa`, `pausada`, `resolvida`, `falhou`, `cancelada`. O grafo permitido está em `team/enums.py` (`CASE_TRANSITIONS`); qualquer outra transição lança `InvalidTransition`. Cada mudança exige a **revisão esperada** (`RevisionConflict` se desatualizada; dois escritores concorrentes têm um único vencedor) e grava, na mesma transação, a demanda, o evento (`case.state-changed`, `case.owner-assigned`, `case.reopened`, `case.opened`) e a linha de outbox. A revisão do evento é a da demanda, então a trilha por demanda é contínua.

Regras adicionais: iniciar atendimento exige responsável; `pausada`, `falhou` e `cancelada` exigem motivo; reabrir `resolvida` ou `falhou` exige motivo e limpa `closed_at`; `cancelada` é final.

## Evidências e prova

`evidence` guarda **fato com fonte, data e confiabilidade** (`verificada`, `declarada`, `inferida`) e tipo (`fato`, `resultado_ferramenta`, `recibo`, `documento`). É somente de acréscimo (gatilhos no banco); correções são novas evidências.

- `resolvida` exige ao menos uma evidência `verificada` de tipo técnico (resultado de ferramenta, recibo ou documento). Notas, mensagens do cliente, evidências declaradas/inferidas e **resumos do modelo não contam**.
- Um agente só registra `verificada` com tipo técnico e hash de conteúdo; o operador pode registrar `verificada` por conta própria.
- Resolução manual: só o operador, com motivo; o evento fica marcado `manual_resolution`.

## Histórico

`case_entry` é somente de acréscimo e distingue `nota_interna` (nunca vai ao cliente), `mensagem_externa` (com direção e canal; registrar não envia nada, o envio é T14) e `resumo_ia` (contexto, não prova). `timeline` junta entradas, evidências e eventos em ordem, com o tipo de cada item preservado. Blocos de raciocínio do modelo (`<think>…</think>`, `<thinking>`, `<reasoning>`) são removidos antes de qualquer gravação (objetivo, motivos, notas, resumos, fatos); texto que seria só raciocínio é recusado. O histórico não tem campo para raciocínio privado.

## Independência do chat

Nada na demanda referencia a sessão do navegador ou a conversa efêmera: fechar a aba não resolve, nem apaga, nem altera estado (testado com novo repositório e serviços sobre o mesmo arquivo, simulando reinício, inclusive reabrir uma demanda `falhou`).

## Acesso

Leitura e escrita aplicam o escopo da T03: `NotFound` uniforme para outra empresa/sem permissão e agentes só com concessões `read`/`write` sobre aquela demanda.

## Limites

Sem interface/HTTP, sem subtarefas (`aguardando_subtarefa` não valida subtarefas abertas até a T11), sem aprovações (`aguardando_aprovacao`, T13) e sem envio externo (T14). A prova exigida para resolver é uma regra mínima; evidências específicas por tipo de demanda virão com as skills de cada cenário.
