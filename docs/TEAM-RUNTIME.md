# Runtime de agentes e roteamento de LLM (T08)

Entrega da issue [#23](https://github.com/rafaelmaiabr/assistente/issues/23): `team/runtime.py` (execução) e `team/llm.py` (clientes, roteador, limitador e políticas de modelo), sobre os módulos anteriores ([agentes](TEAM-AGENTS.md), [skills](TEAM-SKILLS.md), [gateway](TEAM-GATEWAY.md), [conexões](TEAM-CONNECTIONS.md)).

> **O chat atual não usa o runtime.** `assistant_core.make_agent`, `BrowserAgent` e a voz continuam exatamente como estavam (LiveKit + Ollama + MCP demo), e todos os testes deles seguem passando. O runtime é desacoplado da interface e da sessão LiveKit; ligar conversa e voz a execuções persistentes é a T31. `persistence.build_runtime(...)` monta todos os serviços sobre o banco local.

## Uma execução

`AgentRuntime.run(tenant_id, run_id, mensagem, case=…, external=…, hints=…)` executa uma `agent_run` persistida:

1. carrega a **versão fixada** do agente (editar o agente depois da fila não muda a execução; versão desativada nesse intervalo falha com `versao_desativada`);
2. lista as ferramentas pelo gateway (só as concedidas e classificadas) e as instruções das skills concedidas;
3. compila o contexto em camadas (`compile_context`): regras do produto > política/escopo > perfil > skills no system; demanda, mensagens e resultados de ferramentas como dados escapados;
4. escolhe o modelo pela política da empresa e itera: modelo → (ferramentas pelo gateway → resultados como dados) → … até uma resposta de texto;
5. grava o estado final da execução (`concluida`, `falhou`, `cancelada`, `expirada`).

Limites da execução (`agent_run.budget`): **passos** (`max_steps` chamadas ao modelo), **tokens** (`token_budget`, usando o uso informado pelo modelo ou estimativa), **duração** (`deadline_seconds`, que também limita cada chamada) e **chamadas de ferramenta** (limite do gateway, padrão 20). Esgotamento retorna `RunOutcome` com motivo (`passos`, `tokens`, `prazo`) e estado coerente. Resposta vazia do modelo (por exemplo uma chamada de ferramenta malformada) vira `falhou`/`resposta_vazia`; não é tratada como sucesso. `runtime.cancel(run_id)` interrompe a chamada do modelo em andamento e propaga ao gateway (a chamada de ferramenta em andamento é cancelada) e a execução termina como `cancelada`.

## Roteamento por regras explícitas

Política = `model_policy` da empresa, ou uma das locais embutidas (`local-primeiro`, `legacy-local`), que seguem `config.json` (modelo e URL do Ollama). Estrutura: `models` (nome → provedor `ollama`/`externo`, modelo, `base_url`, `connection_id` para externo), `rules` (`{"when": {"task": [...], "complexity": [...]}, "use": modelo}`, avaliadas em ordem, todas as chaves precisam casar), `default`, `fallbacks` e `limits.max_output_tokens`. `route(política, hints)` devolve a lista ordenada de candidatos; indisponibilidade ou tempo esgotado do modelo passa ao próximo (**fallback local configurado**), e `fallback_used`/`errors` registram o que houve. Nenhum LLM decide a rota: `hints` vêm do backend (tarefa, complexidade).

## Concorrência e um modelo só

`GenerationLimiter` permite **uma geração simultânea por endpoint** por padrão (`max_concurrent_generations` em `config.json`, 1 a 8) e é compartilhado por todos os agentes, departamentos e empresas; os clientes HTTP também são compartilhados por endpoint. Não há cópia de modelo por departamento.

## Provedor externo

Desligado por padrão e inexistente nas políticas embutidas. Uma política pode declarar um modelo `externo` ligado a uma conexão com credencial no cofre, mas o roteador o ignora até o operador chamar `enable_external(…, acknowledged=True)`, reconhecendo o aviso: o conteúdo das demandas será enviado a um terceiro e pode gerar custos. Alterar modelos/regras da política desliga de novo e exige novo reconhecimento. A chave vem **somente** do cofre, via conexão e conector registrado; variáveis de ambiente (`OPENAI_API_KEY` etc.) nunca são lidas. Sem credencial, com credencial revogada ou desligado, o fallback é o modelo local; sem nenhum modelo permitido a execução falha com `no_model`.

## Validação

- Testes com LLM falso (26): regras, fallback, esgotamento de orçamento (passos, tokens, prazo), cancelamento do modelo e da ferramenta, concorrência, cliente compartilhado, externo (desligado, reconhecimento, cofre, sem variável de ambiente, revogação) e cliente HTTP contra servidor local.
- Smoke com Ollama real (`scripts/smoke_ollama.py`, `scripts/smoke_team_runtime.py`, opcionais): em 22/10/2026, `qwen2.5:7b` respondeu em ~9 s na primeira chamada (carga do modelo) e o fluxo completo com uma ferramenta **fictícia** via gateway terminou em ~3 s, 3 de 3 execuções, com a ferramenta chamada. Resultados de modelos pequenos variam; não é garantia.
- Achado do smoke: o `qwen2.5:7b` deixa de chamar ferramentas (resposta vazia) quando a descrição da ferramenta começa por um identificador (`erp-demo.consultar …`); o runtime descreve ferramentas com uma frase simples e cita no prompt o mesmo nome que o modelo vê. Há teste de regressão.

## Limites

Sem adaptador MCP real (o gateway continua testado com transporte falso; o smoke usa uma ferramenta fictícia), sem streaming, sem pausa/retomada ou fila durável (T10), sem aprovações humanas (T13) e sem integração com chat/voz (T31). Os tokens são contados pelo uso informado pelo modelo; o orçamento é aplicado depois de cada resposta, então pode ser ultrapassado por uma resposta. Provedor externo foi testado só com cliente falso e um servidor HTTP local, não com um serviço real.
