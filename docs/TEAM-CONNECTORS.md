# Camada de conectores (C1)

Entrega da issue [#101](https://github.com/rafaelmaiabr/assistente/issues/101), parte do acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100): `team/connectors/` (contrato, registro, driver de exemplo) e o kit de teste `tests/connector_contract.py`. Usa conexões e cofre ([TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md)) e o gateway ([TEAM-GATEWAY.md](TEAM-GATEWAY.md)) **sem alterá-los**.

> **Estado real:** só o contrato, o registro e um driver **falso** de exemplo existem. Não há driver real (Bling é #104, IMAP/SMTP é #105), nem transporte MCP real (#102), nem capacidade `erp`/`email` (#103/#105), nem tela (#106/#107). O registro vive em memória e é montado pelo código do backend; nada é persistido, então não há schema de contrato nem migração.

## Conceitos

- **Capacidade** (`erp`, `email`...): o que a empresa quer fazer. `Capability(nome, required, optional, arguments, bindings, idempotent)`: `required` mapeia as ferramentas que todo driver precisa oferecer à sua classe exata de operação (`read/write/send/spend`); `optional` faz o mesmo para ferramentas que o driver pode não suportar (`ConnectorRegistry.unsupported` informa quais faltam, nada é simulado); `arguments`, `bindings` e `idempotent` fixam os argumentos, os campos mostrados na aprovação e a chave de idempotência das ferramentas de contrato. As capacidades com contrato completo são `erp` ([TEAM-CONNECTORS-ERP.md](TEAM-CONNECTORS-ERP.md)) e `email-leitura`/`email-envio` ([TEAM-CONNECTORS-EMAIL.md](TEAM-CONNECTORS-EMAIL.md)).
- **Driver**: o fornecedor que cumpre uma capacidade (Bling, IMAP...). Tem um **descritor** e, se for in-process, um transporte `McpTransport`.
- **Resolução**: `ConnectorRegistry.resolve(conexão, capacidade)` escolhe o driver pelo `provider` da **conexão da empresa**. Agente, skill e modelo nunca escolhem fornecedor.

## Descritor

Validado por `validate_descriptor` antes de qualquer registro (`DescriptorRejected`, mensagens sem segredo):

| Campo | Regra |
|---|---|
| `provider`, `capability`, `version` | identificadores simples (`a-z0-9._-`) e versão `1.0.0` |
| `catalog_key` | opcional; chave da entrada aprovada no catálogo MCP (padrão: o provider). Permite manter uma chave já referenciada por skills |
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
- `local_transport()`: um único transporte `local` para o gateway, que despacha pela chave do catálogo (`catalog_key`, padrão = provider) ao transporte de cada driver; servidor desconhecido se comporta como inexistente.

## Painel de conectores (C6, #106)

Aba **Conectores** da seção Equipe (`frontend/src/team/ConnectorsSection.jsx`), API `GET /api/team/companies/{id}/connectors`, `POST …/connectors/{provider}/connections`, `PUT …/connectors/connections/{id}`, `POST …/connectors/connections/{id}/test` e `GET …/connectors/audit` (`team/connectors/catalog.py`, `setup.py`; `team/web.py`). Só administradores veem a aba e só o operador altera.

- **Lista capacidades e drivers** registrados (`TeamApp.services()` registra `erp`, `email-leitura` e `email-envio`), com as conexões da empresa e o estado **real** de cada uma: não configurada, sem credencial, credencial revogada, desativada, **não testada** (configurada ainda não é "funcionando"), "testada: funcionou" ou "teste falhou". O último teste vem do histórico de configuração; nada é inferido. Capacidade sem driver mostra "nenhum conector instalado" (hoje `erp`: o Bling é a #104).
- **O formulário nasce do descritor** (`fields`): texto, número, URL e senha. Os valores são validados contra o descritor (campo desconhecido, obrigatório ausente, tipo errado). Campos do tipo `secret` vão **só ao cofre** (`set_credential`); os demais vão ao cadastro de conta do driver. Nenhuma resposta, erro ou registro devolve o segredo; ao reconfigurar, senha em branco mantém a atual. O campo é `type=password`, sem autocompletar, e é limpo após cada tentativa.
- **Aviso quando o driver não é MCP**, com a justificativa do descritor.
- **Testar conexão** roda o teste real do driver com a credencial do cofre e grava o resultado.
- **Histórico de configuração** (`connector-audit.jsonl` na pasta de dados, só anexa): quem fez, a ação (`conectar`, `configurar`, `testar`), o fornecedor, a conexão e um código fixo do resultado; **sem segredos, sem valores dos campos e sem texto vindo de servidores**.
- Um driver só pode ser configurado pelo painel se tiver um `SetupAdapter` (`configure`, `current`, `test`); sem ele, o painel diz que ainda não é possível. Hoje: IMAP e SMTP (`team/connectors/mail.py`). Em `127.0.0.1`/`localhost` os adaptadores usam conexão sem TLS (conector local ou servidor de teste); em qualquer outro servidor o TLS é obrigatório, como antes.

Pela conversa ("conectar ao <sistema>"): cartão com botões, formulário seguro e teste real; ver [TEAM-CONNECTORS-CHAT.md](TEAM-CONNECTORS-CHAT.md). O descritor aceita `label`, `aliases` (nomes pelos quais a pessoa chama o sistema) e `steps` (passo a passo mostrado antes da autorização no navegador).

Consulta GET a rotas públicas liberadas pelo operador (ex.: registro de domínio da BrasilAPI), sem gateway ainda: [TEAM-CONNECTOR-HTTP.md](TEAM-CONNECTOR-HTTP.md).

## Como adicionar um conector

1. Copie `team/connectors/example.py`; escreva o descritor (MCP primeiro; fora disso, justifique).
2. Registre a capacidade (se nova) e o driver; chame `attach` e `approve`.
3. Crie uma classe de teste com `ConnectorContractMixin` (veja `ExampleDriverContract` em `tests/test_team_connectors.py`) e implemente `seed`, `probe` e `effects`.
4. Acrescente os testes específicos do fornecedor (fixtures, erros do provedor, limites). O kit não os substitui.
5. Para configurar pelo painel, declare `fields` no descritor e passe um `SetupAdapter` ao `register_driver` (veja `ImapSetup` em `team/connectors/mail.py`): o formulário, a validação, o cofre, o teste e o histórico já vêm da camada.

## Kit de contrato

Para qualquer driver: classificação confiável e coerente com o que o servidor afirma (`describe`), ferramenta inexistente recusada, argumentos desconhecidos/de escopo rejeitados, ferramentas que alteram algo **não têm efeito** sem `dry_run` explícito, resultados nunca repetem o segredo e empresas são isoladas. Um teste do próprio kit confirma que um driver que vaza segredo, ignora dry-run e mistura empresas é reprovado.

## Limites

- A escolha do driver de uma capacidade por empresa é implícita: vale o que a empresa conectou (uma conexão por driver). Não há uma configuração à parte de "driver ativo".
- Largura de celular e Electron não foram conferidos visualmente (a janela de teste não encolhe abaixo de ~680 px); o painel foi conferido em navegador de mesa com um servidor IMAP falso.

- Validado só com o driver de exemplo e fakes; nenhuma conta ou serviço real.
- O kit cobre o que é comum; aprovação exata (#28), limites de tempo/tamanho e revalidação continuam sendo do gateway e já têm testes próprios. Os testes aqui não passam por skills nem pelo gateway completo.
- Erros tipados do transporte MCP real são da #102.
