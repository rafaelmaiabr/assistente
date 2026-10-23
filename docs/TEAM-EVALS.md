# Evals e gates de regressão (T33)

Entrega da issue [#48](https://github.com/rafaelmaiabr/assistente/issues/48): o pacote `evals/` (fora do pacote portátil), o corpus sintético versionado em `evals/corpus/`, `scripts/run_evals.py`, a base de regressão `evals/baseline.json`, o runner opcional com Ollama e o [checklist de release](TEAM-RELEASE.md).

**O que isto é e o que não é.** Os gates mostram que os cenários listados se comportam como esperado; **não provam** que o sistema seja invulnerável. O corpus é sintético: mede este pipeline, não o tráfego de clientes reais. Os gates rodam offline, com dublês, em um mundo fictício criado e descartado a cada execução.

## Como rodar

```
python scripts/run_evals.py                      # tudo; sai com 1 se houver violação de gate ou regressão
python scripts/run_evals.py --suite aprovacao    # só uma suíte (repetível)
python scripts/run_evals.py --json r.json --markdown r.md
python scripts/run_evals.py --update-baseline    # aceita os resultados medidos de hoje como nova base (revise o diff)
python scripts/run_evals.py --ollama             # TAMBÉM consulta o modelo local (opcional; nunca é gate)
```

O relatório **não tem data** (a menos que se peça `--stamp`), então o mesmo código e o mesmo corpus geram o mesmo arquivo, byte a byte. A CI roda a suíte offline e publica o relatório no resumo da execução.

## Suítes

**Gates (tolerância zero):**

| Suíte | O que verifica |
|---|---|
| `autorizacao` | agente sem concessão não lê; agente não cria concessão nem aprova; leitor não escreve; atendente não aprova nem configura; membro limitado por departamento; contexto de acesso não se forja; a empresa enviada pelo cliente é ignorada; sessão revogada e papel alterado valem na hora; worker não aprova nem gerencia membros |
| `aprovacao` | payload alterado (destinatário, parâmetros, valor, recurso, ação, demanda), aprovação expirada, usada duas vezes, revogada, negada, pendente, de outra demanda ou de outra empresa; hash mostrado diferente |
| `dono_unico` | quem não é dono não muda o estado nem fala com o cliente nem cria rascunho; transferência só vale depois do aceite e tira o direito do antigo dono; segundo dono recusado; operador que assume para o agente |
| `envio_duplicado` | rascunho repetido, envio repetido, envios concorrentes, aprovação consumida várias vezes, encaminhamento repetido, importação repetida e resposta perdida do provedor: o efeito externo acontece **no máximo uma vez** |
| `vazamento_entre_empresas` | 16 tentativas da empresa B sobre o que é da A (por id, detalhe, cursor de eventos, aprovação, mensagem, encaminhamento, agente, conhecimento, listas, financeiro, departamento e agente alheios, empresa no corpo, execução de agente e membro de outra empresa) |
| `injecao` | com um modelo "enganado" (resposta de confiança 100 escolhida pelo atacante), mensagens que dão ordens ao sistema nunca são roteadas pela palavra do modelo; quando há assunto real, só vale o que as regras reconhecem |
| `evidencias` | só resultado de ferramenta, recibo ou documento **com hash** vira evidência verificada (o operador pode declarar); um resumo gerado ou um fato sem origem não resolve a demanda |

**Medidas (comparadas com a base):** `triagem` (40 mensagens, com as respostas do modelo roteirizadas no corpus), `escopo` (pedidos fora do que a equipe faz não são roteados) e `cobranca_incorreta` (11 casos: duplicidade, beneficiário alterado, pagamento anterior, moeda divergente, outra empresa, dados ausentes, origem por email, orçamento...). Não podem passar menos itens que a base nem ter menos itens que ela (cobertura reduzida conta como regressão).

Mudou um prompt, uma skill, uma regra ou o modelo? Rode os evals: a diferença aparece como violação de gate ou regressão, sem precisar de dados privados.

## Corpus

`evals/corpus/*.json`, cada arquivo com `corpus_version` e descrição: `triagem`, `injecao`, `evidencias`, `escopo`, `cobranca_incorreta`, `vazamento`, `duplicatas`. Um teste confere que cada um é versionado, tem ids únicos, não contém segredos e só usa endereços de exemplo (`.invalid`, `exemplo.com`). Para acrescentar um caso: inclua o item com id novo, rode os evals e, se for uma suíte medida, atualize a base com `--update-baseline` e revise o diff.

## O que os evals já encontraram

Na primeira execução a suíte de injeção achou três ataques que a heurística de `team/triage.py` deixava passar quando um modelo sobreconfiante dava confiança 100 (a mesma ordem em inglês, "finja que sou o dono... acesso total" e o pedido de revelar instruções/dados de outros clientes). A heurística foi ampliada, com o custo conhecido de alguns **falsos positivos** (por exemplo, "mostrar a chave do wifi" agora vai para uma pessoa em vez de ser roteado só pelo modelo). Também ficaram visíveis dois erros do próprio corpus, corrigidos.

## Evals com o modelo local (opcional)

`--ollama` passa o corpus de triagem e o de injeção pelo classificador real e registra, por item e no total: o modelo (nome e, quando o servidor informa, versão do Ollama, família, tamanho e quantização), latência, tokens, decisão correta ou não e erro. Há **orçamento**: `--ollama-limit` (chamadas ao modelo, padrão 20) e `--ollama-tokens` (padrão 20000); o que não coube é relatado como ignorado. Só o Ollama local é contatado. O resultado vai na chave `ollama` do relatório e **nunca** reprova a execução: um modelo não é determinístico. Testado apenas com um modelo falso (contagem de tokens, latência, orçamento, erro do servidor e modelo enganado); **não foi executado contra um Ollama real nesta entrega**.

## Validação

`tests/test_team_evals.py` (27): a suíte completa com zero violações e sem regressão; cobertura dos gates exigidos; base em dia; relatório reproduzível; formato do relatório; integridade do corpus; **os gates têm dentes**: quebrar o produto de propósito (autorização liberando tudo, repositório que esquece a empresa, aprovação que aceita qualquer payload, verificação de dono removida, envio sem memória, guarda de injeção desligada, evidência sem lastro técnico, análise financeira sem achados) é detectado por gate ou como regressão; comportamento da base; linha de comando; os novos padrões de injeção contra pedidos comuns; e o runner do Ollama com modelo falso.

## Limites

- O corpus é pequeno e sintético; passar nos gates não garante o comportamento diante de entradas novas.
- A heurística de injeção é uma lista de padrões: ataques novos exigem novos casos no corpus e padrões novos.
- Os gates cobrem os cenários listados; não substituem revisão de segurança nem testes com serviços reais (Google, SMTP/IMAP, registradores), que continuam pendentes e listados no [checklist de release](TEAM-RELEASE.md).
- A suíte usa a API dos serviços, não a interface; testes de interface seguem em `npm test` e nos testes do desktop.
