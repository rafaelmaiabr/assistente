# Contexto do chat: poda de resultados e limite de tamanho (#133)

Issue [#133](https://github.com/rafaelmaiabr/assistente/issues/133). Origem: [REFERENCIA-HERMES.md](REFERENCIA-HERMES.md) (estudo de referência; nada foi copiado). Código em `chat_context.py`, ligado em `assistant_core.py`.

Um modelo local pequeno perde qualidade quando o histórico enche de listagens de email e respostas de rotas. Duas medidas determinísticas, sem usar modelo:

## Poda de resultados antigos (`prune`)

- Antes de cada resposta, os resultados de ferramentas **anteriores às duas últimas mensagens da pessoa** e com mais de **200 caracteres** têm o texto trocado por um marcador: "resultado antigo removido para economizar contexto; consulte a ferramenta de novo se precisar".
- A chamada e o seu resultado **continuam no histórico**; só o texto longo sai. O que a pessoa disse e o que o assistente respondeu não são tocados.
- Uma rodada de ferramenta ainda sem resposta fica dentro da parte mantida (nunca é podada).
- Resultados curtos ficam; podar de novo não muda nada. O contexto de entrada não é alterado (a função devolve uma cópia).
- Ligação: `ChatAgent.on_user_turn_completed` poda o contexto do turno (o que o modelo lê) e o histórico guardado.

## Limite de tamanho por resultado (`fit`)

- Um resultado de ferramenta de email ou de rota com mais de **6.000 caracteres** vai ao modelo cortado, como `{"aviso": ..., "dados_nao_confiaveis": true, "parcial": "..."}`. O aviso diz para pedir um recorte menor.
- Resultado que não pode ser serializado vira um erro simples.

## Limites

- Os números (2 mensagens, 200 e 6.000 caracteres) são constantes em `chat_context.py`, **sem configuração na tela**; não foram calibrados contra um modelo real. O ganho de qualidade não foi medido: o PR só garante o comportamento por testes com contexto sintético.
- Os resultados de ferramentas MCP externas não passam pelo corte de tamanho (só os de email e de rota). A poda vale para qualquer resultado no histórico.
- Compressão com resumo e busca em conversas são a issue [#141](https://github.com/rafaelmaiabr/assistente/issues/141).
