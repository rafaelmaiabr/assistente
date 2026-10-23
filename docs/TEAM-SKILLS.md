# Skills declarativas e concessões (T06)

Entrega da issue [#21](https://github.com/rafaelmaiabr/assistente/issues/21): `team/skills.py`, sobre contratos, acesso, conexões e agentes ([TEAM-CONTRACTS.md](TEAM-CONTRACTS.md), [TEAM-ACCESS.md](TEAM-ACCESS.md), [TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md), [TEAM-AGENTS.md](TEAM-AGENTS.md)). Ainda não há tela nem HTTP (T15) nem chamada real de ferramentas (gateway: T07).

## Manifesto (`skill.json`)

Campos: `manifest_version` (1), `id`, `version` (X.Y.Z), `description`, instruções (`instructions` **ou** `instructions_file`), `inputs`, `outputs`, `required_tools` (`{"tool": "servidor.ferramenta", "operations": [...]}`), `risks` (`dados_pessoais`, `dados_financeiros`, `leitura_email`, `envio_externo`, `pagamento`, `alteracao_dominio`), `settings` (nomes das configurações que a skill espera) e `fictitious`. O hash do conteúdo (`content_hash`) identifica a versão revisada. A skill é um catálogo global; **contas, segredos e concessões são por empresa** e nunca fazem parte do pacote.

## Importação local sem executar nada

`SkillService.review_package(caminho)` (pasta com `skill.json` ou o próprio arquivo) valida e **não altera nada**; devolve o que será permitido: ferramentas e operações, riscos, configurações, versão anterior e **permissões adicionais** em relação à versão instalada.

Recusados: JSON inválido, campos desconhecidos, tamanhos acima de 64 KB (manifesto), 32 KB (arquivo de instruções), 256 KB (pacote) e mais de 20 arquivos; subpastas, atalhos e qualquer arquivo que não seja texto (`.json`, `.md`, `.txt`) — ou seja `.py`, `.ps1`, `.cmd`, `.exe`, `.dll`, `.js`, `.sh`; manifestos com chaves de comando (`command`, `scripts`, `postinstall`, `entrypoint`, `hooks`…); `instructions_file` que não seja um nome simples de arquivo regular **dentro** do pacote (`../`, caminho absoluto, subpasta, atalho). Nenhum código do pacote é importado ou executado.

`install(operador, caminho, hash_revisado)` exige o hash visto na revisão (o conteúdo não pode mudar entre revisar e instalar), é idempotente, impede reinstalar a mesma versão com conteúdo diferente ou instalar versão menor, e **recusa upgrade que pede permissões novas** sem `accept_new_permissions=True` (mostrando quais).

Pacotes de demonstração claramente fictícios (`fictitious: true`, descrição "[DEMONSTRACAO - dados ficticios]"): `team/skills_demo/secretaria`, `suporte` e `financeiro`. O pacote Windows leva a pasta `team/` inteira.

## Concessões (`skill_grant`)

`grant_skill` (operador) vincula: **versão fixa** da skill (o id do documento), agente, conexão autorizada, operações (subconjunto do que a skill declara), recurso e validade opcionais. Regras:

- skills com ferramentas exigem conexão MCP ativa cujo servidor do catálogo aprovado ofereça cada ferramenta requerida (`servidor.ferramenta`);
- agente e conexão precisam existir na mesma empresa; duplicatas ativas são recusadas;
- publica `config.skill-granted` na outbox; `attach=True` publica uma nova versão do agente com a concessão.

**Atualizar a skill não amplia concessões:** a nova versão é outro documento e a concessão continua apontando para a versão antiga e suas operações. Para a nova versão é preciso nova concessão.

## Uso e bloqueios (`authorize_use`)

O gateway (T07) chamará `authorize_use(contexto, agente, skill, operação, conexão, recurso, agent_version)`. Só retorna uma concessão que esteja nas `skill_grant_ids` do agente (na versão fixada da execução, se informada), não revogada nem expirada, com skill ativa, operação e recurso cobertos e conexão ativa e coincidente. **Qualquer outro caso levanta a mesma `SkillDenied`**, sem revelar o motivo. `revoke_grant` bloqueia novas chamadas imediatamente, inclusive para execuções presas a versões antigas do agente; `set_skill_status(inativo)` bloqueia todas as concessões daquela versão.

## Limites

Revisão é uma API (sem tela); `risks` e `settings` são informativos (a configuração por empresa de `settings` vem depois). A aplicação por chamada de ferramenta e os limites por execução são da T07. Pacotes de terceiros executáveis continuam fora do escopo e exigiriam desenho e revisão próprios.
