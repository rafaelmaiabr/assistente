# Camada de conectores (C1)

Entrega da issue [#101](https://github.com/rafaelmaiabr/assistente/issues/101), parte do acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100): `team/connectors/` (contrato, registro, driver de exemplo) e o kit de teste `tests/connector_contract.py`. Usa conexões e cofre ([TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md)) e o gateway ([TEAM-GATEWAY.md](TEAM-GATEWAY.md)) **sem alterá-los**.

> **Estado real:** só o contrato, o registro e um driver **falso** de exemplo existem. Não há driver real (Bling é #104, IMAP/SMTP é #105), nem transporte MCP real (#102), nem capacidade `erp`/`email` (#103/#105), nem tela (#106/#107). O registro vive em memória e é montado pelo código do backend; nada é persistido, então não há schema de contrato nem migração.

## Conceitos

- **Capacidade** (`erp`, `email`...): o que a empresa quer fazer. `Capability(nome, required, optional, arguments, bindings, idempotent)`: `required` mapeia as ferramentas que todo driver precisa oferecer à sua classe exata de operação (`read/write/send/spend`); `optional` faz o mesmo para ferramentas que o driver pode não suportar (`ConnectorRegistry.unsupported` informa quais faltam, nada é simulado); `arguments`, `bindings` e `idempotent` fixam os argumentos, os campos mostrados na aprovação e a chave de idempotência das ferramentas de contrato. A primeira capacidade com contrato completo é `erp` ([TEAM-CONNECTORS-ERP.md](TEAM-CONNECTORS-ERP.md)).
- **Driver**: o fornecedor que cumpre uma capacidade (Bling, IMAP...). Tem um **descritor** e, se for in-process, um transporte `McpTransport`.
- **Resolução**: `ConnectorRegistry.resolve(conexão, capacidade)` escolhe o driver pelo `provider` da **conexão da empresa**. Agente, skill e modelo nunca escolhem fornecedor.

## Descritor

Validado por `validate_descriptor` antes de qualquer registro (`DescriptorRejected`, mensagens sem segredo):

| Campo | Regra |
|---|---|
| `provider`, `capability`, `version` | identificadores simples (`a-z0-9._-`) e versão `1.0.0` |
| `protocol` | `mcp` (preferido), `imap`, `smtp`, `http`, `outro` |
| `mcp_transport` | `http`/`sse`/`stdio`/`local`; obrigatório só no MCP |
| `non_mcp_justification` | 30–500 caracteres, **obrigatória** fora do MCP e proibida no MCP |
| `auth` | `type` (`none`/`secret`/`oauth`) e `scopes` |
| `tools` | por ferramenta, a mesma especificação do catálogo aprovado: `operation`, `arguments`, limites. Sem classificação, nome genérico de execução (`shell`, `exec`, `run-command`...) ou argumento reservado (`tenant_id`, `token`...) é recusado |
| `dry_run` | toda ferramenta que não é `read` declara o argumento booleano `dry_run`; o driver **simula por padrão** |
| `idempotency_arguments` | opcional: ferramenta → argumento que carrega a chave de idempotência do fornecedor |
| `fields` | campos de configuração (`text`/`secret`/`url`/`number`) que o formulário seguro da #107 vai gerar; segredos só vão ao cofre |

## Registro e ligação

`ConnectorRegistry`: `register_capability`, `register_driver(descritor, transporte)` (exige capacidade conhecida, ferramentas obrigatórias com a classe certa, provider único), `get/drivers/resolve`. Ligação ao que já existe, sem tocar nesses módulos:

- `attach(connections)`: registra o provider como conector para o cofre (`use_credential`).
- `approve(connections, operator, provider)`: o operador aprova o driver MCP no catálogo, com a classificação confiável do descritor. Drivers não-MCP não entram no catálogo.
- `local_transport()`: um único transporte `local` para o gateway, que despacha pela chave do catálogo (= provider) ao transporte de cada driver; servidor desconhecido se comporta como inexistente.

## Como adicionar um conector

1. Copie `team/connectors/example.py`; escreva o descritor (MCP primeiro; fora disso, justifique).
2. Registre a capacidade (se nova) e o driver; chame `attach` e `approve`.
3. Crie uma classe de teste com `ConnectorContractMixin` (veja `ExampleDriverContract` em `tests/test_team_connectors.py`) e implemente `seed`, `probe` e `effects`.
4. Acrescente os testes específicos do fornecedor (fixtures, erros do provedor, limites). O kit não os substitui.

## Kit de contrato

Para qualquer driver: classificação confiável e coerente com o que o servidor afirma (`describe`), ferramenta inexistente recusada, argumentos desconhecidos/de escopo rejeitados, ferramentas que alteram algo **não têm efeito** sem `dry_run` explícito, resultados nunca repetem o segredo e empresas são isoladas. Um teste do próprio kit confirma que um driver que vaza segredo, ignora dry-run e mistura empresas é reprovado.

## Limites

- Validado só com o driver de exemplo e fakes; nenhuma conta ou serviço real.
- O kit cobre o que é comum; aprovação exata (#28), limites de tempo/tamanho e revalidação continuam sendo do gateway e já têm testes próprios. Os testes aqui não passam por skills nem pelo gateway completo.
- Erros tipados do transporte MCP real são da #102.
