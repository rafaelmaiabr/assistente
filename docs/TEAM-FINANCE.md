# Skill Financeiro: análise de cobranças e reconciliação (T24)

Entrega da issue [#39](https://github.com/rafaelmaiabr/assistente/issues/39): `team/finance.py` (importação e análise), `team/finance_mcp.py` (servidor MCP local somente leitura), contrato `financial_record`, migração `0016`, pacote de demonstração `team/skills_demo/financeiro-analise`. Usa conhecimento ([TEAM-KNOWLEDGE.md](TEAM-KNOWLEDGE.md)), skills ([TEAM-SKILLS.md](TEAM-SKILLS.md)), gateway ([TEAM-GATEWAY.md](TEAM-GATEWAY.md)) e aprovações ([TEAM-APPROVALS.md](TEAM-APPROVALS.md)).

Estado: **análise e proposta apenas**. Nenhum pagamento, cobrança ou envio é feito aqui, e não existe conector bancário ou de ERP. Não há tela e o servidor não está ligado ao `TeamApp`. Só foi validado com dados sintéticos; nenhum dado real foi usado.

## Registros importados de forma explícita

Só o **operador** importa (`FinanceService.import_csv`, texto CSV de até 1 MB e 5 000 linhas). Colunas: `tipo`, `contraparte`, `beneficiario`, `pagador`, `documento`, `referencia`, `valor`, `moeda`, `emissao`, `vencimento`, `origem`, `data_da_fonte` (obrigatória), `observacao`. Cada registro guarda **fonte, data da fonte, origem** (`erp`, `importacao`, `email`, `manual`), lote e quem importou.

- **Nada é inferido.** Valor exige ponto decimal e nenhum separador de milhar (`1234.56`); `1.234,56` é rejeitado como ambíguo. Moeda (ISO de 3 letras) é obrigatória quando há valor e proibida sem valor. Datas só em `AAAA-MM-DD`. Linhas inválidas são **relatadas com o motivo**, nunca adivinhadas. Valor e moeda podem ficar vazios: o registro existe, mas é "dados incompletos".
- Reimportar conteúdo idêntico é ignorado; o mesmo documento com dados diferentes é mantido e gera alerta de duplicidade.
- A `observacao` é texto não confiável: se parecer instruir o assistente, é descartada com aviso; e nunca sai nas consultas.

## Tipos mantidos separados

`fatura_fornecedor` (a pagar), `cobranca_cliente` (a receber), `pagamento_realizado` (a empresa já pagou), `orcamento_fornecedor` (**não é dívida**) e `renovacao` (**aviso, não obrigação confirmada**). Totais confiáveis são por moeda e **nunca somam moedas diferentes**; registros sinalizados ficam de fora.

## Achados e bloqueios

| Código | Quando | Bloqueia proposta |
|---|---|---|
| `dados_incompletos` | sem valor/moeda, ou fatura sem beneficiário | sim |
| `moeda_divergente` | fatura e orçamento/registro relacionado (`referencia`/`documento`) em moedas diferentes | sim |
| `fatura_duplicada` | mesmo documento (ou mesmo valor+vencimento) em mais de um registro; lista no que diferem | sim |
| `possivel_pagamento_anterior` | existe pagamento realizado correspondente | sim |
| `beneficiario_alterado` | beneficiário difere do histórico da contraparte (gravidade crítica) | sim |
| `cobranca_de_outra_empresa` | pagador não corresponde ao nome da empresa (ou apelidos informados) | sim |
| `valor_divergente_do_orcamento`, `beneficiario_sem_historico`, `origem_email_nao_verifica_identidade` | atenção | não, mas exigem verificação |
| `orcamento_nao_e_divida`, `renovacao_nao_e_obrigacao_confirmada` | informativo | não |

Havendo bloqueio, a proposta vira `pedir_esclarecimento` com a lista de perguntas (por exemplo, "confirme por um canal independente do email"). Sem bloqueio: `solicitar_aprovacao_pagamento` (fatura do fornecedor), `propor_cobranca_ao_cliente` (só se vencida; caso contrário acompanhar), `avaliar_renovacao`, `comparar_orcamentos`. Toda proposta traz `executa: false` e `exige_aprovacao`; a de pagamento nomeia o conector específico necessário (inexistente hoje) e `verificacao_fora_do_email` quando a origem é email ou não há histórico do beneficiário. **Identidade, beneficiário e autorização nunca são validados só por um email.**

## Ferramentas (somente leitura) e documentos

Catálogo `financeiro-local` (transporte `local`, ver [TEAM-IMAP.md](TEAM-IMAP.md)): `consultar-registros` (contraparte, tipo, documento, limite) e `analisar-pendencias` (contraparte, documento). Não existem ferramentas para importar, pagar, cobrar ou enviar; argumentos extras são recusados pelo gateway. A análise inclui **documentos autorizados** recuperados pelo conhecimento da empresa (T17) com título, fonte, versão e data de publicação, marcados como não confiáveis e dependentes do departamento do agente. Tudo o que vem dos dados importados (contrapartes, beneficiários) é devolvido com `dados_nao_confiaveis: true`.

## Validação

`tests/test_team_finance.py` (18 testes) com `tests/data/finance_synthetic.csv` (13 registros fictícios): moeda divergente, duplicata de fatura, beneficiário alterado, cobrança de outra empresa, evidência insuficiente, pagamento anterior, orçamento e renovação, cobrança vencida, origem por email; importação com valores ambíguos/moeda/datas/origens/tipos inválidos, reimportação, nota com instrução, cabeçalho/tamanho/permissões, separador `;`, arquivamento; isolamento entre empresas; chamadas pelo gateway com fonte e data, ausência de ferramentas de pagamento e de importação, argumentos extras recusados.

## Limites

Sem conciliação automática com extrato bancário (só registros importados), sem câmbio (moedas nunca convertidas), sem leitura de PDF/NF-e, sem tela de importação e sem conector de pagamento. A comparação de pagador usa o nome cadastrado da empresa (mais apelidos informados na chamada interna); nomes muito diferentes podem gerar falso alerta, que resulta em pedido de esclarecimento e não em ação.
