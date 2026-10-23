# Central de demandas (T16)

Entrega da issue [#31](https://github.com/rafaelmaiabr/assistente/issues/31): aba **Demandas** na seção Equipe, API em `team/web.py` e modelos de leitura em `team/overview.py`. Usa demandas, encaminhamentos, aprovações, mensagens e controle do operador ([TEAM-CASES.md](TEAM-CASES.md), [TEAM-HANDOFFS.md](TEAM-HANDOFFS.md), [TEAM-APPROVALS.md](TEAM-APPROVALS.md), [TEAM-OUTBOUND.md](TEAM-OUTBOUND.md)) e o painel ([TEAM-PANEL.md](TEAM-PANEL.md)).

## O que se vê

- **Lista por empresa**, com fase, responsável, aprovações pendentes e sinal de reconciliação; filtro por fase; quem precisa de uma pessoa aparece primeiro.
- **Fases**: *Em andamento*, *Aguardando o cliente*, *Aguardando uma pessoa* (aprovação pendente, pausada/sob controle do operador, resultado desconhecido a reconciliar, encaminhamento a revisar ou fila humana da triagem), *Falhou*, *Concluída* e *Cancelada*. Uma demanda encerrada mantém a fase de encerramento mesmo com reconciliação pendente (o sinal continua visível).
- **Detalhe**: responsável, subtarefas, trilha de transferências/delegações (com quem propôs e para quem), aprovações, mensagens ao cliente com estado (`enviada` = aceita pelo provedor; `desconhecido` destacado), e linha do tempo com notas, mensagens, evidências (fonte, data, confiabilidade; resumos de IA marcados como "não é prova") e eventos.
- **Aprovação**: cartão com a ação concreta, destinatário(s), valor/moeda, recurso, parâmetros, validade, estado e identificação (hash) do conteúdo. **Aprovar** exige marcar que conferiu ação/destinatário/valor e envia o hash exibido (o servidor recusa se não coincidir); negar e revogar exigem motivo.
- **Intervenção**: assumir, pausar, devolver à equipe e rejeitar, sempre com motivo e com a **revisão** vista na tela; se outra pessoa mudou a demanda, o servidor responde 409 e a tela pede para recarregar. A checagem de permissão é do backend (`OperatorControl`, `ApprovalService`), não da interface.
- **Reconciliação**: operações com resultado desconhecido mostram alerta e formulário para registrar, após conferir o sistema externo, que aconteceu ou não (nada é desfeito automaticamente).

## Atualização, reconexão e snapshot autoritativo

`GET …/cases/{id}` devolve o **snapshot** completo e um `cursor` (maior `seq` de eventos). `GET …/events?after=N[&case_id=]` devolve eventos novos em ordem (`seq` global crescente) e o novo cursor; o cliente **deduplica por id**, nunca recua o cursor e usa os eventos apenas como aviso de que algo mudou: a verdade vem sempre de um novo snapshot, então atualizações duplicadas ou fora de ordem não corrompem a tela. A tela consulta a cada 3 s (pausa com a aba oculta); em falha mostra "Sem conexão. Tentando reconectar…", espera com recuo exponencial (até 30 s) e, ao voltar, **recarrega lista e detalhe** em vez de confiar em estado local. Recarregar a página também parte de um snapshot.

## Empresas e acesso

Toda rota leva o id da empresa; empresa inexistente, id de outra empresa e demanda invisível respondem o mesmo 404; a lista, os eventos e a caixa de atenção (`…/attention`) só trazem dados da empresa; o feed de eventos é separado por empresa.

## Limites

Sondagem periódica, não push (WebSocket/SSE ficam para quando houver necessidade medida). Sem criação de demandas pela tela nem triagem/atribuição manual além do endpoint `assign`; o envio de mensagens, o conteúdo de rascunhos e a classificação de ferramentas não são editados aqui. O envio real depende de transportes que ainda não existem (T18/T22). Verificado em navegador nas larguras ≈ 767 px e ≈ 500 px (menu recolhido) com demandas semeadas em várias fases; Electron e rolagem de longas linhas do tempo não foram verificados visualmente. Dois operadores simultâneos só existem como teste de revisão (o modo local tem um operador).
