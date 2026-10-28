# O chat não deve afirmar o que não faz (#121)

O modelo local pequeno (`qwen2.5:7b`) ignora o "nunca invente" do prompt: disse ter acesso à internet, "estou listando" sem chamar ferramenta, ofereceu mover spam e descreveu um botão que não existe. Prompt sozinho não resolve, então parte da honestidade passou para o servidor. **Nenhuma capacidade nova foi adicionada**; só se diz com precisão o que já existe.

## O que o servidor faz

1. **Capacidades reais no prompt** (`honesty.capabilities_text`, chamado por `assistant_core.agent_parts`): o parágrafo "Capacidades reais nesta sessão" é gerado a partir das ferramentas **registradas** (`tool.info.name`), não de texto fixo. Sem ferramenta de integração o cartão não é listado; sem rota ligada não há consulta; com a caixa configurada ou não, os limites seguem sempre ditos (não acessa a internet em geral; não move, apaga, arquiva nem marca emails; não envia sem aprovação; não cria botões, cartões ou links).
2. **Resposta fixa para o que não existe** (`honesty.parse` + `honesty.answer`, em `web_server.py`, **sem passar pelo modelo**): "Consegue acessar a internet?", "mova os spam", "apague meus emails", "marque como lido", "envie sem aprovação" e "acesse https://…" recebem uma resposta curta e verdadeira, dizendo que nada foi feito e o que é possível. A resposta sobre internet lista as rotas que o operador ligou (#120), se houver. Só frases curtas (até 120 caracteres) e simples entram aqui; perguntas do tipo "como faço para apagar um email no Gmail?" continuam indo ao modelo.
3. **Botão que ninguém mostrou** (`honesty.fake_button`): se o texto do modelo cita `[Conectar …]`, "clique no cartão" ou "botão abaixo" e o servidor **não** emitiu cartão, formulário ou rascunho naquele turno, a tela recebe um aviso (evento `warning`) ao lado da resposta.
4. **Ação anunciada sem ferramenta** (`honesty.announces_action`): se o texto diz "estou listando/consultando/verificando…" e nenhuma ferramenta foi executada no turno, o aviso diz que nada foi feito e sugere a frase direta ("liste meus emails").

Os avisos aparecem como o banner de aviso já existente; a resposta do modelo **não é apagada nem reescrita**, e o aviso não é falado em voz alta.

## O que isto não garante

- É uma lista **fechada** de frases e padrões. Um modelo pequeno ainda pode errar fora deles: afirmar algo falso com outras palavras, prometer sem usar os verbos reconhecidos, descrever um botão de outro jeito.
- O aviso de ação anunciada pode errar para os dois lados: "vou ler" dito sem intenção de agir gera aviso; uma promessa com verbo fora da lista não gera.
- A resposta fixa é por frase curta. O mesmo pedido dentro de um texto longo vai ao modelo.
- Não troca o modelo nem a configuração, e não torna o modelo mais capaz de usar ferramentas.
- Não adiciona mover, apagar ou marcar emails: isso seria outra issue, de escrita, com aprovação.

## Testes

`tests/test_honesty.py` (padrões, falsos positivos e avisos), `tests/test_assistant_capabilities.py` (prompt de uma sessão sem email, com email, com `solicitar_integracao` e com rota; precisa do livekit) e `tests/test_web_honesty.py` (servidor com agente falso: o modelo nunca é chamado nas respostas fixas, o aviso aparece, não vaza para o turno seguinte e não aparece depois de uma ferramenta real).
