# Comunicação externa: rascunhos, envio e dono único (T14)

Entrega da issue [#29](https://github.com/rafaelmaiabr/assistente/issues/29): `team/outbound.py` (`OutboundGateway`, `outbound_handler`), migração `0012`. Usa demandas, encaminhamentos, aprovações, conexões e a fila ([TEAM-CASES.md](TEAM-CASES.md), [TEAM-HANDOFFS.md](TEAM-HANDOFFS.md), [TEAM-APPROVALS.md](TEAM-APPROVALS.md), [TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md), [TEAM-QUEUE.md](TEAM-QUEUE.md)).

> **Estado real:** o gateway é testado com um provedor **falso**. Não existe ainda transporte SMTP/IMAP (T22/T18); o contrato `OutboundTransport` é o ponto de encaixe. Nenhuma mensagem real é enviada por esta entrega.

## Rascunho ≠ envio

`create_draft` guarda destinatários, assunto, texto, conta de saída e canal (`email`↔conexão `smtp`, `chat`↔conexão `outro`), mais o **dono atual e a versão de propriedade** da demanda. Só o responsável atual (ou o operador por ele) escreve rascunhos; o rascunho é dado editável (`update_draft`) e **não toca o provedor**. Não existe caminho de uma nota interna, de um resumo de IA ou de um texto externo para uma mensagem: o texto precisa ser fornecido explicitamente (raciocínio do modelo é removido; segredos embutidos são recusados pelo contrato).

Fluxo: `rascunho` → `submit` (valida política e cria a aprovação do envio) → `aguardando_aprovacao` → operador aprova → `dispatch` → `enviando` → `enviada` | `falhou` | `desconhecido`. Também `bloqueada` e `cancelada`. `withdraw` devolve ao rascunho (revoga a aprovação) para editar.

## O que é conferido imediatamente antes de enviar

1. a mensagem está aprovada e o texto continua idêntico ao aprovado (o hash do rascunho faz parte do payload aprovado);
2. **dono atual** e **versão de propriedade** iguais aos do rascunho, demanda ativa, não pausada nem sob controle do operador;
3. conta de saída ativa e credencial válida (o segredo vai só ao conector);
4. política de destinatários **agora**: cada endereço deve ser de um contato *vinculado* ou de um domínio permitido na conta (`allowed_recipient_domains`); remetentes apenas observados não bastam, e o limite é 5 destinatários;
5. a aprovação é consumida (uso único) e o dono é conferido de novo logo antes da chamada.

Qualquer falha bloqueia a mensagem (`bloqueada`, com o motivo) e grava nota interna; nada é enviado.

## Serialização, deduplicação e tabela como outbox

`outbound_messages` é a outbox: chave de deduplicação única por empresa (repetir `create_draft` com a mesma chave devolve o mesmo rascunho; a chave também é enviada ao provedor), identificador do provedor único, e **no máximo uma mensagem `enviando` por demanda** garantido por índice no banco (a outra recebe `Busy` e tenta depois). Envios simultâneos da mesma mensagem resultam em um só envio.

## Troca de dono

A transferência aceita (T11) chama um gancho na mesma transação que **bloqueia rascunhos e mensagens pendentes do responsável anterior** (`bloqueada`/`dono_mudou`) e registra nota. O novo responsável usa `revalidate`: a mensagem volta a rascunho sob ele, **sem a aprovação antiga**, e precisa ser submetida e aprovada de novo; o autor da mensagem registrada passa a ser o novo dono. O responsável anterior não consegue revalidar, escrever rascunhos nem responder. Se o dono muda *depois* de a aprovação ser consumida e *antes* da chamada, a última conferência impede o envio (efeito `falhou`).

## Resultado desconhecido

Se a chamada saiu e a resposta não chegou (timeout, resposta inválida, queda), a mensagem fica **`desconhecido`**: não é reenviada, o histórico da demanda **não** ganha mensagem de saída, a aprovação registra efeito `desconhecido` e a mensagem aparece em `needing_reconciliation`. A reconciliação é por `auto_reconcile` (o provedor reconhece a chave de deduplicação) ou pelo operador (`resolve_unknown`: `enviada` exige o identificador do provedor; `nao_enviada` marca `falhou`). Mensagens presas em `enviando` por mais de 10 minutos viram `desconhecido` (`mark_stuck`). Recusa explícita do provedor é `falhou`; credencial/conta indisponível antes de sair também. `enviada` significa **aceita pelo provedor**, nunca entregue ou lida.

## Fila

`outbound_handler` trata jobs `enviar_mensagem`: espera a aprovação sem abrir efeito, abre o efeito `enviar:<id>` do T10 antes de despachar (queda no meio vai para `reconciliar`, sem reenvio), adia com `Busy` e conclui com o identificador do provedor.

## Limites

Sem SMTP/IMAP reais (T22/T18), sem entrada de emails (T21) e sem interface (T16). Chat usa o mesmo contrato mas não há transporte de chat. A política de destinatários é por conta, não por contato; respostas "para o remetente" exigem vincular o endereço ao contato. A garantia é de que nada sai sem aprovação exata e dono válido; o provedor pode ainda duplicar se ignorar a chave de deduplicação.
