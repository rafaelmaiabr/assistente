# Empresas, contatos e política de acesso (T03)

Entrega da issue [#18](https://github.com/rafaelmaiabr/assistente/issues/18). Serviço interno em `team/access.py` e `team/service.py`, sobre os contratos ([TEAM-CONTRACTS.md](TEAM-CONTRACTS.md)) e a persistência ([TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md)). Não há tela nem endpoint HTTP para isso ainda (painel: T15); o chat atual não usa o serviço.

## Modelo local: o que é e o que não é

Modo local tem **um operador confiável** (id estável `usr_…` em `team-data/operator.json`). O isolamento entre empresas é **lógico, dentro do processo**. Isto **não** é autenticação SaaS, não separa usuários e não resiste a outro processo da mesma conta do Windows, que consegue abrir o arquivo SQLite. Acesso remoto e RBAC: T32.

## Contextos de acesso

Funções do serviço só aceitam um `OperatorContext` ou `AccessContext`, criados exclusivamente por `AccessService`:

- `local_operator()` → operador local;
- `tenant_context(operador, tenant_id)` → contexto da empresa (papel `operator`: acesso total *dentro* dela);
- `agent_context(operador, tenant_id, run_id)` → contexto de uma execução persistida e ativa; o principal é o agente da execução (papel `agent`).

Dicionários, `tenant_id`, papel ou principal vindos da LLM, de mensagem ou de argumento de ferramenta são dados, nunca contexto: construir um contexto à mão é recusado, e `create`/`update` rejeitam `id`, `tenant_id`, `kind`, `revision` etc. como entrada. O backend (runtime T08, fila T10) é quem deriva o `tenant_id` do registro persistido da execução.

## Política default-deny

- Operador: pode tudo dentro da própria empresa, incluindo gerenciar concessões. Nunca vê outra empresa.
- Agente: **nada** sem uma `access_grant` explícita (`principal_id`, `resource_kind`, `resource_id` ou nulo para todos da empresa, `actions` entre `read`, `write`, `relate`, `export`, validade opcional). Agentes não criam nem leem concessões, não alteram empresas e só alcançam os tipos de `RESOURCE_KINDS`.
- Consulta por ID aplica o mesmo escopo que listas. `relate` é exigido sobre o recurso apontado ao criar vínculos ou ligar endereços; `export` é separado de `read`.
- **Sem vazamento de existência:** ID de outra empresa, ID sem permissão e ID inexistente produzem o mesmo `NotFound` (mesma mensagem). Listas omitem em silêncio. Busca de contato por endereço, exportação (`export`) e eventos (`events`) usam as mesmas verificações. Isolamento reforçado no banco por chaves estrangeiras compostas `(tenant_id, id)`.

## Contatos

Identidade e endereço são registros diferentes. `contact` é a pessoa/organização; `contact_address` é um e-mail ou telefone:

- `observado`: remetente visto numa mensagem, **sem contato** — não identifica ninguém (`find_contact_by_address` devolve vazio).
- `vinculado`: o operador (`link_address`) associou o endereço a um contato; só então a busca resolve a identidade. `unlink_address` desfaz.
- Endereços são únicos por empresa, sem diferenciar maiúsculas. `contact_link` relaciona contato e departamento (`cliente`, `fornecedor`, `interno`, `outro`), uma relação por par.

Empresas, departamentos e contatos são **desativados**, não apagados (preserva histórico); só vínculos, endereços e concessões podem ser removidos. Atualizações usam revisão esperada (`RevisionConflict`).

## Limites

Sem interface ou API HTTP, sem auditoria (T30) nem eventos de configuração (T09+). Dados pessoais de contatos ficam sem criptografia no SQLite local. A migração `0002` acrescenta as tabelas sem alterar dados existentes (com cópia `.bak` antes).
