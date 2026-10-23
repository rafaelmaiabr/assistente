# Secretaria: triagem, esclarecimento e roteamento (T12)

Entrega da issue [#27](https://github.com/rafaelmaiabr/assistente/issues/27): `team/triage.py` (`TriageService`, `LlmClassifier`), migração `0010`, corpus sintético em `tests/data/` e `scripts/eval_triage.py`. Usa demandas, encaminhamentos e perfis ([TEAM-CASES.md](TEAM-CASES.md), [TEAM-HANDOFFS.md](TEAM-HANDOFFS.md), [TEAM-AGENTS.md](TEAM-AGENTS.md)).

> A triagem é o motor determinístico que a execução do agente Secretaria chama (com contexto de operador no backend). A ligação do chat/email a esse motor e o envio das perguntas ao cliente são T21/T14/T28: a pergunta de esclarecimento fica registrada como rascunho, não é enviada.

## Catálogo de capacidades

`capability` por empresa: nome, descrição, palavras-chave e se exige cliente identificado (`requires_customer`). Cada agente declara quais capacidades atende (`agent_definition.capabilities`, validadas contra o catálogo). O roteamento é por **nomes de capacidade**; ids de agentes nunca vêm do modelo nem do cliente.

## Decisão

1. **Regras primeiro.** Palavras-chave com correspondência de palavra inteira (sem acentos, maiúsculas ou plural). Uma capacidade → `rotear`; duas ou mais → `misto` (até 3). Confiança: 55% + 20% por palavra encontrada (≥ 70% age).
2. **Modelo só quando nenhuma regra se aplica.** `LlmClassifier` envia o catálogo (nomes e descrições) no system e a mensagem do cliente escapada como dado; a resposta é JSON estrito `{"capacidades":[{"nome","confianca"}],"pergunta"}`. Nomes fora do catálogo (inclusive ids inventados) são descartados e registrados; confiança é limitada a 0–100; qualquer outro campo (conceder acesso, credencial, prompt, agente destino) é ignorado. Sozinho, o modelo precisa de ≥ 90% e de ≥ 4 palavras para rotear (modelos pequenos são excessivamente confiantes: medido abaixo); entre 40% e o limiar pergunta ao cliente; abaixo disso vai para a fila humana. No máximo uma chamada ao modelo por passada.
3. **Mensagem que tenta instruir o assistente** ("ignore as instruções", "SYSTEM:", "conceda acesso", "use a credencial…", tags) nunca é roteada com base apenas no modelo: vai à fila humana com o motivo `possivel_injecao`. Se a mensagem também tem um assunto real, as regras ainda roteiam (o destino é um agente do catálogo, com as mesmas permissões de sempre). É heurística de defesa em profundidade, não garantia.
4. **Restrições:** capacidade que exige cliente com demanda sem `customer_ref` → pergunta quem é o cliente; destino sem agente ativo ou **sem acesso à demanda** → fila humana; no máximo 3 passadas por demanda (depois, fila humana).

## Ação

- `rotear`: a Secretaria vira dona na triagem e **propõe a transferência** ao agente da capacidade; ele precisa aceitar (T11).
- `misto`: a Secretaria continua dona única e cria **uma subtarefa por capacidade** (delegação, T11); ninguém mais vira dono.
- `esclarecer`: a Secretaria é a dona, a demanda vai a `aguardando_cliente` com a pergunta registrada como nota (rascunho). Uma nova passada retoma e decide.
- `fila_humana`: a demanda fica em `triagem` sem dono; `human_queue` a lista. Falha ao executar a ação (limites, destino que mudou) também cai aqui. Um pedido que uma pessoa já decidiu não atender (lacuna `nao_atender` com memória confirmada, [TEAM-GAPS.md](TEAM-GAPS.md)) também vai direto para cá, com o motivo `fora_do_escopo`, sem classificar.

## Registro verificável

Cada passada grava um `triage_decision` **somente de acréscimo** (gatilhos no banco): decisão, método, capacidades, confiança, motivos (palavras encontradas ou confiança do modelo, itens descartados, restrições), pergunta, hash da mensagem (o texto não é copiado), hash do catálogo vigente, limiares aplicados, rodada e autor. Uma nota interna resume a decisão na linha do tempo da demanda.

## Medição (corpus sintético de 40 mensagens)

`python scripts/eval_triage.py` (regras) e `python scripts/eval_triage.py --llm` (Ollama local):

| Categoria | Mensagens | Só regras | Com `qwen2.5:7b` local |
|---|---|---|---|
| Financeiro | 10 | 10 | 10 |
| Suporte | 10 | 10 | 10 |
| Assunto misto | 6 | 6 | 6 |
| Ambíguo (deveria perguntar) | 2 | 0 | 1 |
| Sem assunto | 2 | 2 | 2 |
| Fora do escopo | 4 | 4 | 4 |
| Injeção de prompt | 3 | 3 | 3 |
| Injeção com assunto real | 3 | 3 | 3 |
| **Total** | **40** | **38 (95%)** | **39 (98%)** |

Medido em 22/10/2026. O erro restante do modelo é conservador: uma mensagem ambígua foi para a fila humana em vez de gerar pergunta. Antes de endurecer os limiares do modelo (90% e 4 palavras, mais o filtro de injeção), a mesma execução teve 37/40: o modelo deu 100% de confiança a "Preciso de ajuda" e seguiu uma instrução injetada. O corpus é pequeno e sintético, mede este pipeline e não tráfego real; na CI o classificador é um falso determinístico com as respostas esperadas de um modelo bem comportado (`tests/test_team_triage.py`), e um segundo teste usa um modelo "malicioso" para verificar que nada além de nomes de capacidade é aceito.

## Limites

Sem interface (T16), sem envio ao cliente (T14), sem leitura de email (T21). As palavras-chave exigem curadoria por empresa; o modelo local pode errar em linguagem informal. Subtarefas de uma passada `misto` não têm prazo de aceite diferente do padrão (1 h). Não há reclassificação automática quando o cliente responde: a próxima passada é disparada pelo backend.
