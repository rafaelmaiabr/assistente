# Perfis de agentes e compilação de instruções (T05)

Entrega da issue [#20](https://github.com/rafaelmaiabr/assistente/issues/20). `team/agents.py` (definições, versões, execuções) e `team/prompting.py` (montagem de contexto), sobre os módulos anteriores ([contratos](TEAM-CONTRACTS.md), [persistência](TEAM-PERSISTENCE.md), [acesso](TEAM-ACCESS.md)). Ainda não há tela nem HTTP (painel: T15) e nenhum LLM é chamado (runtime: T08).

## Definição de agente

Campos: empresa e departamento, nome, objetivos (`goals`), personalidade (`tone`, `initiative`), escopo e fora de escopo, política de encaminhamento (`routing_policy`: `encaminhar` ou `esclarecer`), destinos de encaminhamento, concessões de skill, política de modelo (`model_policy_ref`) e perfil de voz (`voice_profile_ref`). Referências são validadas dentro da empresa: departamento, agentes de destino e concessões (que precisam ser do próprio agente) devem existir; perfil de voz deve existir nos perfis de voz atuais; política de modelo deve ser conhecida (lista provisória até a T08). Um agente não encaminha para si mesmo.

`AgentService` (somente operador escreve): `create_agent`, `update_agent`, `get_agent`, `list_agents`. Agentes só leem com concessão explícita (default-deny, como no T03).

## Versões e histórico

Cada criação ou alteração **publica uma versão**: definição, snapshot imutável (`agent_profile_version`, com hash do conteúdo) e um evento `config.agent-published` com linha de outbox, tudo na mesma transação. O banco impede alterar ou apagar o conteúdo de uma versão publicada (só o `status` muda) e impede criar uma `agent_run` para versão inexistente ou desativada.

- `start_run` fixa a versão usada (`agent_version` da execução). Editar o agente depois não muda execuções anteriores nem as versões antigas.
- `set_version_status` desativa/reativa uma versão apenas para **novas** execuções; o conteúdo continua legível (e compilável com `require_active=False` para reprodução). Agente desativado não inicia execuções.

## Compilação de instruções (`compile_context`)

Ordem de autoridade: **regras do produto > política e escopo > perfil > skills**, e o prompt de sistema contém só essas quatro camadas (texto do produto ou do operador, escritas e revisadas por quem configura). Demanda, mensagens do cliente, emails, anexos e resultados de ferramentas são **dados**: vão em mensagens de usuário, rotulados com a fonte (`<dados_externos fonte= tipo=>`), com `<`, `>` e `&` escapados (não dá para forjar ou fechar delimitadores), com limite de tamanho e de quantidade. Nada externo vira system prompt.

- O texto não concede permissões: a lista "Ferramentas desta execução" vem do gateway (T07) e o prompt afirma que citar uma ferramenta não a autoriza.
- Fora do escopo: com `encaminhar` e destinos conhecidos, o prompt manda oferecer o encaminhamento a esses agentes; caso contrário (ou com `esclarecer`), manda pedir esclarecimento. O comportamento do modelo é avaliado em T33; esta camada só garante a instrução.
- Perfis cujo texto tenta anular regras ou aprovações ("ignore as regras", "sem aprovação", etc.) são recusados na criação. É defesa em profundidade por padrões de texto, **não** uma garantia; a autorização real está no gateway e nas políticas.

## Limites

Sem interface/HTTP; sem chamada ao modelo; política de modelo provisória; avaliação de qualidade das respostas fora do escopo (T33). Skills e concessões chegam na T06; até lá `skill_grant_ids` só aceita concessões já existentes.
