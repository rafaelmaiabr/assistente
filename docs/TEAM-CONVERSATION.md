# Conversa e voz vinculadas ao agente e à demanda ativa (T31)

Entrega da issue [#46](https://github.com/rafaelmaiabr/assistente/issues/46): `team/conversation.py` (vínculo), turnos e cancelamento no WebSocket (`web_server.py`), `frontend/src/conversationModel.js` (regras puras), `frontend/src/ConversationLink.jsx` (barra de vínculo e controles), e o avatar do Electron (`desktop/`). Usa a central de demandas ([TEAM-CASES-CENTER.md](TEAM-CASES-CENTER.md)), encaminhamentos ([TEAM-HANDOFFS.md](TEAM-HANDOFFS.md)) e controle do operador ([TEAM-APPROVALS.md](TEAM-APPROVALS.md)).

**Limite importante:** o chat continua sendo o assistente local. O vínculo **identifica** empresa, demanda e agente responsável e faz a **voz** acompanhar o responsável; ele não faz o chat executar o agente da equipe (isso é do runtime/fila, T08/T10). Validado com agente e voz falsos; o app real foi aberto só para conferir que a barra renderiza sem erros no navegador.

## Vínculo capturado no início do turno

A pessoa pode vincular a conversa a uma demanda (`attach` com `company_id` e `case_id`) ou desvincular (`detach`). A cada mensagem, o servidor lê o vínculo **no banco** (empresa, demanda, agente dono, versão, perfil de voz, "aguardando uma pessoa", pausada, controle do operador) e o fixa para o turno: todos os eventos do turno (`status`, `message`, `audio`, `tool`, `warning`, `error`) levam o número do turno e, mensagens e áudio, um vínculo compacto. Trocar ou remover o vínculo **durante** uma resposta é recusado (`busy`): nada muda no meio do turno por clique concorrente. Demanda inexistente, de outra empresa, ou persistência desligada: recusado; se a demanda some, a conversa volta ao assistente local.

## Transferência só vale depois do aceite

O dono (e a voz) vêm da demanda. Uma transferência só **proposta** não muda nada; depois do aceite, o próximo turno anuncia `binding` com motivo `mudou` **antes** de qualquer mensagem do turno, e a síntese usa o perfil de voz do novo dono. Mudança de fase (por exemplo pausada ou aguardando pessoa) atualiza a barra, mas não é um novo "interlocutor".

## Eventos atrasados ou de outra demanda

O cliente descarta (`applyEvent`) eventos de turnos já encerrados ou cancelados e **mensagens e áudio cujo vínculo é de outra demanda ou empresa**, inclusive se a empresa mudou durante a síntese. Eventos antigos sem número de turno continuam funcionando.

## Quatro controles diferentes

| Controle | O que faz | O que não faz |
|---|---|---|
| **Parar fala** | interrompe o áudio que toca | não cancela a geração |
| **Interromper resposta** | envia `cancel`; o servidor cancela a geração e confirma com `cancelled` | não para o áudio nem mexe no microfone |
| **Pausar demanda** | chama a pausa do operador na demanda (com a revisão esperada) | não encerra a chamada |
| **Microfone** | liga/desliga a captura | **nunca é ligado por evento**: trocar de agente ou de vínculo jamais ativa o microfone (testado) |

## Avatar

O avatar do Electron mostra escuta, processamento, fala e "Aguardando uma pessoa", além do agente e da demanda, e tem **Abrir demanda**. O chat publica só ids no formato exato do backend e rótulos curtos e limpos (`sanitizeStatus` descarta o resto); o avatar **não fornece ids**: ao clicar, o processo principal usa a última referência validada publicada pelo chat confiável e manda ao chat o comando `open-case`, que revalida o formato, para o microfone e abre a seção Equipe na demanda certa. Nenhum canal IPC novo foi criado.

## Latência e texto

Mede-se o tempo do envio até a primeira resposta do assistente (por turno, últimas 20, mostrado na barra). O texto continua sempre disponível: nada depende do áudio.

## Reconexão

Ao reconectar, o servidor começa sem vínculo; o cliente guarda a escolha da pessoa e envia `attach` de novo sozinho, sem tocar no microfone.

## Validação

`tests/test_conversation_ws.py` (8): turnos marcados, vínculo vindo do banco, voz do dono, transferência só após aceite com anúncio antes das mensagens, vínculo imutável durante o turno, casos desconhecidos ou de outra empresa, "aguardando pessoa" e pausada, cancelamento sem áudio tardio e sessão reutilizável, reconexão. `frontend/tests/conversation.test.js` (8): captura no início do turno, eventos fora de ordem e cancelados, outra demanda/empresa, assistente local sem vínculo, latência, microfone nunca ligado, quatro controles separados, reconexão, rótulos e avatar. `desktop/tests/policy.test.cjs`: ids e rótulos do avatar. Regressão: suíte Python completa, `npm test`/`npm run build`, testes do desktop, `node --check` e o `--smoke-test` do Electron (executado localmente: passou).

## Limites

O chat não executa o agente da equipe; sem sincronização de áudio ao vivo entre dois agentes; a barra não lista aprovações pendentes (vê-se na seção Equipe); "Pausar demanda" usa um motivo fixo; sem teste manual com microfone e voz reais nesta entrega.
