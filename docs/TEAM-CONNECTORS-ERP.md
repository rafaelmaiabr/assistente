# Capacidade ERP (C3)

Entrega da issue [#103](https://github.com/rafaelmaiabr/assistente/issues/103), parte do acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100): `team/connectors/erp.py` (contrato e modelo de cobrança), `team/connectors/erp_ledger.py` (reconciliação), `team/connectors/erp_fake.py` (driver de referência falso). Usa a camada de conectores ([TEAM-CONNECTORS.md](TEAM-CONNECTORS.md)), o gateway e as aprovações ([TEAM-GATEWAY.md](TEAM-GATEWAY.md), [TEAM-APPROVALS.md](TEAM-APPROVALS.md)) e a análise financeira ([TEAM-FINANCE.md](TEAM-FINANCE.md)).

> **Estado real:** o contrato, a lógica de reconciliação, o driver **falso** e (desde a [#110](https://github.com/rafaelmaiabr/assistente/issues/110)) o **livro de cobranças durável** no banco da equipe (SQLite e PostgreSQL). **Não há ERP real** (Bling é a [#104](https://github.com/rafaelmaiabr/assistente/issues/104)) e **nenhuma rota HTTP recebe eventos** nem há polling agendado: `Reconciler.ingest_webhook` e `apply_snapshot` são chamados por quem tiver o dado. Nada aqui emite cobrança, move dinheiro ou fala com cliente por conta própria.

## O contrato `erp`

`ERP` (`Capability`) é registrada com `ConnectorRegistry.register_capability`; todo driver `erp` o cumpre.

| Ferramenta | Classe | Obrigatória | Observação |
|---|---|---|---|
| `consultar-clientes`, `consultar-cobrancas`, `status-cobranca` | `read` | sim | argumentos de contrato (busca, cliente/documento/status/limite, documento) |
| `consultar-pedidos` | `read` | não | |
| `emitir-cobranca`, `alterar-cobranca` | `write` | não | cliente, beneficiário, documento, valor em centavos, moeda, vencimento, chave de idempotência e `dry_run` |

- O que o driver **não** suporta é declarado, não simulado: `ConnectorRegistry.supports` e `unsupported` informam as ferramentas opcionais ausentes.
- Ferramentas de escrita só são aceitas se ligarem `cliente` ao destinatário, `valor_centavos` ao valor e `moeda` à moeda da aprovação, com `cliente`, `beneficiario`, `documento`, `valor_centavos`, `moeda` e `chave` obrigatórios, e `chave` declarada como chave de idempotência. O gateway já inclui **todos** os argumentos no hash do que o aprovador vê: mudar cliente, valor, moeda, beneficiário, documento ou chave exige nova aprovação.
- Emitir/alterar passa sempre pelo caminho de escrita do gateway (aprovação exata, uso único, efeito registrado), **inclusive com `dry_run`**: uma simulação sem aprovação exigiria uma ferramenta de leitura própria, que o driver pode oferecer além do contrato. O padrão do driver é simular (`dry_run` ausente = simular).

## Modelo de cobrança

`normalize_charge`: documento, cliente, beneficiário, **valor em centavos inteiros** e **moeda explícita**, status (`emitida`, `vencida`, `paga`, `estornada`, `cancelada`; `pendente` é sinônimo de `emitida`), vencimento `AAAA-MM-DD`, e `extras` com os campos do fornecedor, limitados a 4 KB, com segredos redigidos e nunca interpretados.

## Reconciliação (`Reconciler`)

| Situação | Resultado |
|---|---|
| Webhook com assinatura inválida | `assinatura_invalida`; nada é lido, gravado ou revelado |
| Mesmo `event_id` de novo | `duplicado` |
| Mesmo fato com outro id | `ja_aplicado`; **uma só** baixa e um só recibo |
| Status pendente/emitida | sem baixa |
| Estorno antes do pagamento | `aguardando_ordem`; aplicado quando o pagamento chega |
| Evento mais antigo que o estado atual | `obsoleto` |
| Cliente, valor ou moeda diferentes da cobrança | `divergente`; nada aplicado; revisão pendente |
| Pagamento em cobrança cancelada (e o inverso) | `divergente` (`estado_contraditorio`) |
| Documento desconhecido ou de **outra empresa** | `desconhecida` |
| Mensagem do cliente ("já paguei") | `alegacao_registrada`; o estado **não muda**; qualquer fonte que não seja `webhook` ou `polling` devolve `fonte_nao_prova_pagamento` |

- Autenticação do evento: `verify_hmac_sha256(segredo, corpo_bruto, assinatura, prefix=...)` (comparação em tempo constante). O segredo vem do cofre, via o conector autorizado; o formato real de assinatura de cada fornecedor é do driver.
- O polling autorizado usa `apply_snapshot` com id determinístico, então repetir a consulta não duplica baixa.
- Recibos (`recibos`) guardam tipo, valor, moeda, momento, fonte e o evento que os comprovou.

## Emissão: resposta perdida bloqueia a repetição

`run_emission` (para drivers) reserva a chave de idempotência antes de chamar o ERP. Se a chamada falhar depois de poder ter saído (qualquer erro, inclusive cancelamento), a chave fica `desconhecido`: repetir devolve `UnknownResult` ("verifique no ERP antes de repetir"). Só `resolve_emission(..., found=cobrança)` (confirmada no ERP) ou `found=None` (ausência verificada, libera nova tentativa) destrava. `NotSent` marca que a chamada comprovadamente não saiu, e a chave é liberada. Reusar a chave com outros dados é recusado.

## Armazenamento durável (#110)

`Reconciler(store)` aceita qualquer armazenamento com `get`/`put` por (empresa, tipo, fornecedor, chave) e `atomic(empresa)`: `MemoryStore` (testes, um processo) ou `LedgerStore(repository)` (`team/connectors/erp_store.py`), sobre o banco da equipe.

- Um novo contrato, `erp_ledger_entry` (tipos `charge`, `event` e `emission`), na tabela `erp_ledger_entries` (migração `0025`, com equivalente PostgreSQL). **A unicidade (empresa, tipo, fornecedor, chave) é do banco**: o mesmo evento ou a mesma chave de emissão não entram duas vezes.
- Cada operação que grava roda numa única transação da empresa: ou o evento, o estado da cobrança e o recibo são gravados juntos, ou nada é. Uma falha no meio deixa a cobrança como estava.
- Entregas simultâneas do mesmo evento (ou do mesmo fato com ids diferentes) produzem **uma** baixa e **um** recibo. Quem perde a corrida desfaz a transação, tenta de novo (até 3 vezes) e vê o resultado do vencedor (`duplicado`/`ja_aplicado`). Eventos que não mudam nada não reescrevem a cobrança. Isso só vale porque a gravação é conferida contra a **revisão que a mesma transação leu** (a primeira leitura de cada chave): reler a linha na hora de gravar deixava, em PostgreSQL, quem perdeu a corrida sobrescrever o vencedor (issue [#124](https://github.com/rafaelmaiabr/assistente/issues/124); `tests/test_team_erp_concurrency.py` reproduz a intercalação sem depender de tempo).
- Reiniciar não perde estado, recibos, eventos já vistos nem emissões desconhecidas.
- Uma transação pertence a uma única empresa; o repositório recusa documento de outra empresa e a linha de uma empresa não é alcançável por outra.
- Sem `store`, o `Reconciler` usa memória (não persiste), como antes.

## Análise financeira

`emission_blockers(analise)` devolve os achados de `FinanceService.analyze` (#39) que bloqueiam proposta (fatura duplicada, beneficiário alterado, cobrança de outra empresa, possível pagamento anterior...). A proposta de emissão não deve ir à aprovação enquanto houver algum; as regras continuam só na análise financeira.

## Limites

- Validado só com o driver falso e dados sintéticos; nenhum ERP, conta ou webhook real.
- Sem rota HTTP de eventos nem polling agendado (dependem de como o fornecedor entrega eventos: #104).
- As linhas do livro **não pertencem a uma demanda**: a exclusão controlada e a retenção por demanda não as alcançam (o backup do banco as inclui). Política de retenção do livro não foi definida.
- O armazenamento durável foi validado com SQLite (inclusive concorrência entre threads com conexões próprias). **A paridade com PostgreSQL só roda na CI** (`scripts/run_tests_postgres.py` inclui `tests.test_team_erp`); não havia PostgreSQL local nesta entrega. **Correção a esta afirmação:** a #110 foi mergeada com a CI do PostgreSQL verde, mas a garantia "uma baixa e um recibo" **não valia** em PostgreSQL (o teste de concorrência só falhava dependendo do tempo e passou por sorte); a #124 corrigiu e passou a testar a intercalação de forma determinística.
- Movimentação bancária e pagamento a fornecedor continuam **manuais**; os recibos e estados técnicos servem ao atendimento direto pelo Financeiro.
- Cobertura parcial do aceite da [#42](https://github.com/rafaelmaiabr/assistente/issues/42) (T27): o contrato, a reconciliação com contrato falso e a persistência. Faltam o ERP/sandbox real e a entrada de eventos.
