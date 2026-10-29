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

## Erros de ferramenta e parada com resumo (#134)

- `tool_guard.py`: todo erro das ferramentas de email, de rota e de integração volta ao modelo no mesmo formato: `erro`, `pode_tentar_de_novo` e `diga_a_pessoa`. O texto é limpo antes: endereços viram `[endereco]`, caminhos locais `[caminho]` e sequências longas parecidas com chave `[omitido]`. Uma exceção vira "falhou por um motivo interno", sem detalhes.
- Chamada **idêntica** (mesma ferramenta e mesmos argumentos) que falhou 2 vezes nos últimos 120 segundos não é executada de novo: volta um erro dizendo que não adianta repetir. Outros argumentos, um sucesso ou o passar dos 120 s liberam a chamada.
- Argumento inválido: o LiveKit já não executa a ferramenta e devolve `Error parsing arguments ...` ao modelo; isso **não** foi padronizado neste PR (o texto vem do LiveKit e pode repetir o valor que o modelo mandou).
- Limite de passos (`max_tool_steps`, padrão 3 do LiveKit): o LiveKit força uma última resposta sem ferramentas. Se o modelo ainda assim não diz nada depois de usar ferramentas, o `BrowserAgent` envia um resumo fixo do que foi consultado e do que falhou, em vez de silêncio (antes era só o erro genérico "o modelo não retornou uma resposta"). Sem nenhuma ferramenta usada, esse erro continua.
- Limites: o resumo é montado dos nomes das ferramentas, não pelo modelo; as ferramentas MCP externas não passam pelo `Guard`; nada disso foi testado com um modelo real, só com modelo e sessão falsos.

## Prompt em camadas (#135)

- Ordem fixa em `assistant_core.agent_parts`: **estável** (`BASE`: identidade e regras de honestidade, igual em todo estado), **índice** das capacidades realmente registradas (`capabilities_text`, #121) e **volátil** (bloco da caixa de email ativa, cartão de integração e rotas liberadas).
- As instruções longas de cada capacidade saíram do prompt e foram para a descrição da própria ferramenta (`preparar_resposta`, `ler_email`, `solicitar_integracao`); o bloco `CATALOG` foi removido porque repetia o índice.
- `BrowserAgent.refresh()` troca as ferramentas e, só se o texto mudou, as instruções; como o prefixo estável é o mesmo, trocar a caixa ou conectar uma integração não o reescreve.
- Medido em caracteres (o mesmo cenário antes e depois): prompt sem email configurado 1707 → 1468; email com envio 2112 → 1498; email com envio, cartão e uma rota 3053 → 2197. As descrições das ferramentas cresceram (957 → 1385 no cenário completo), então o total que o modelo lê nesse cenário caiu de 4010 para 3582 (cerca de 11%). Caracteres, não tokens.
- Não foi acrescentada hora ao prompt: ele não a tem hoje, e incluí-la seria uma funcionalidade nova. Os testes de honestidade (#121) e os da equipe não tiveram expectativa alterada. Nada foi medido com o modelo real; se o texto mais curto melhora as respostas continua sem evidência.

## Limites

- Os números (2 mensagens, 200 e 6.000 caracteres) são constantes em `chat_context.py`, **sem configuração na tela**; não foram calibrados contra um modelo real. O ganho de qualidade não foi medido: o PR só garante o comportamento por testes com contexto sintético.
- Os resultados de ferramentas MCP externas não passam pelo corte de tamanho (só os de email e de rota). A poda vale para qualquer resultado no histórico.
- Compressão com resumo e busca em conversas são a issue [#141](https://github.com/rafaelmaiabr/assistente/issues/141).
