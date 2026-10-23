# Contratos de domínio da equipe (T01)

Entrega da issue [#16](https://github.com/rafaelmaiabr/assistente/issues/16). Define **contratos validáveis**; ainda não há persistência, runtime, fila ou painel (T02 em diante). O backend atual (chat, voz, MCP demo) não usa estes módulos.

Implementação: pacote `team/`, somente biblioteca padrão, sem I/O.

| Arquivo | Conteúdo |
|---|---|
| `team/ids.py` | IDs opacos `<prefixo>_<32 hex>` (`new_id`, `is_id`) |
| `team/enums.py` | Estados e vocabulários; grafo permitido de transições da demanda |
| `team/schema.py` | Especificação executável de cada tipo, `validate`, `validate_bundle`, detecção de segredos, exportação JSON Schema |
| `team/legacy.py` | Importação pura do `config.json` antigo |
| `team/samples.py` | Exemplos sintéticos determinísticos |
| `team/schemas/*.schema.json` | JSON Schema gerado (estrutural; `validate` é a autoridade) |
| `team/examples/team-bundle.json` | Exemplo gerado, sem dados reais |

Regenerar schemas e exemplo: `python scripts/export_team_schemas.py`. Um teste falha se os arquivos versionados divergirem da especificação.

## Regras dos contratos

- **IDs opacos e versão.** Todo documento tem `kind`, `schema_version` (hoje `1`) e `id` com prefixo do tipo. Nomes legíveis (`skill_key`, `model_policy_ref`, `voice_profile_ref`) são *referências por nome*, nunca identificadores internos. Versão desconhecida é rejeitada (`unsupported_schema_version`); campos desconhecidos também.
- **Empresa e demanda.** `tenant_id` é obrigatório em todo dado de negócio. `case_id` é obrigatório em tarefa, aprovação, transferência/delegação, evidência, comando e evento (exceto mensagens `config.*`). Skills são um catálogo global declarativo e não carregam `tenant_id`.
- **Sem segredo embutido.** Conexões guardam `credential_ref` (`crd_…`) para o cofre; nunca o valor. A validação rejeita chaves como `password`/`token`/`api_key`/`authorization`, valores com formato de chave (sk-, ghp_, Bearer, chave privada…) e URLs com usuário/senha. Chaves terminadas em `_ref`, `_id`, `_ids`, `_hash` e `token_budget` são aceitas.
- **Revisão.** Entidades mutáveis têm `revision` (inicia em 1), `created_at` e `updated_at`. Comando leva `expected_revision` (controle otimista); o evento registra a `revision` resultante.
- **Envelopes.** Comando e evento carregam `id`, `type`, `tenant_id`, `case_id`, `correlation_id`, `causation_id` (id do comando/evento que causou), `actor_id` (`usr_`, `agt_` ou `sys_`) e timestamp. Comando inclui `idempotency_key`.
- **Conceitos separados.** `agent_definition` (configuração versionada), `agent_run` (execução com orçamento), `skill_definition` (instruções + hash + ferramentas), `skill_grant` (concessão explícita de uma versão para um agente, conexão, recurso e operações) e `connection` (provedor + `credential_ref`). Instruções de uma skill não concedem acesso: só `skill_grant`.
- **Estados.** Demanda: `recebida, triagem, em_execucao, aguardando_cliente, aguardando_aprovacao, aguardando_subtarefa, pausada, resolvida, falhou, cancelada`, com o grafo em `CASE_TRANSITIONS` (`cancelada` é final; reabrir `resolvida`/`falhou` é transição explícita). T09 aplica o grafo com revisão atômica. Há enums também para tarefa, execução, aprovação, transferência, operação e conexão.
- **Contatos e acesso (T03).** `contact`, `contact_address` (identidade separada do endereço; `observado` ou `vinculado`), `contact_link` e `access_grant` (default-deny). Detalhes em [TEAM-ACCESS.md](TEAM-ACCESS.md). Tipos novos são aditivos: `schema_version` continua 1.
- **Conexões (T04).** `connection` ganhou `catalog_entry_id` e `scopes` (opcionais), `credential_ref` agora referencia um `credential` (metadados), e há o catálogo global `mcp_catalog_entry`. Detalhes em [TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md).
- **Perfis de agentes (T05).** `agent_definition` ganhou `goals` e `routing_policy` (opcionais) e há `agent_profile_version`, snapshot imutável de cada versão publicada. Detalhes em [TEAM-AGENTS.md](TEAM-AGENTS.md).
- **Skills (T06).** `skill_definition` ganhou campos opcionais de manifesto (descrição, entradas/saídas, riscos, configurações, operações por ferramenta) e `skill_grant` ganhou `revoked_at`. Detalhes em [TEAM-SKILLS.md](TEAM-SKILLS.md).
- **Gateway (T07).** `mcp_catalog_entry` ganhou `tool_specs` (classificação e regras de argumentos por ferramenta, definidas pelo operador). Detalhes em [TEAM-GATEWAY.md](TEAM-GATEWAY.md).
- **Políticas de modelo (T08).** `model_policy` (modelos, regras de roteamento, fallbacks, limites; provedor externo desligado por padrão). Detalhes em [TEAM-RUNTIME.md](TEAM-RUNTIME.md).
- **Demandas (T09).** `case` ganhou `priority` e `origin` (opcionais) e a regra de responsável único após a triagem; `evidence` ganhou tipo, confiabilidade e autor; novo `case_entry` (histórico somente de acréscimo). Detalhes em [TEAM-CASES.md](TEAM-CASES.md).
- **Encaminhamentos (T11).** `handoff` ganhou envelope (`objective`, `expected_result`, `evidence_ids`, `return_to`), `attention` e `resolution` (opcionais). Detalhes em [TEAM-HANDOFFS.md](TEAM-HANDOFFS.md).
- **Triagem (T12).** Novos `capability` e `triage_decision` (somente de acréscimo); `agent_definition` ganhou `capabilities` (opcional). Detalhes em [TEAM-TRIAGE.md](TEAM-TRIAGE.md).
- **Aprovações (T13).** `approval_request` ganhou payload normalizado, destinatário, valor/moeda, política, decisão, consumo e resultado do efeito; o estado final é `consumida`; `case` ganhou `operator_control`. Detalhes em [TEAM-APPROVALS.md](TEAM-APPROVALS.md).
- **Comunicação externa (T14).** Novo `outbound_message` (rascunho/outbox); `case` ganhou `owner_version`; `connection` ganhou `allowed_recipient_domains`. Detalhes em [TEAM-OUTBOUND.md](TEAM-OUTBOUND.md).
- **Conhecimento e memória (T17).** Novos `knowledge_document` (versionado, com fonte e validade) e `memory_item` (proposta/confirmada, escopo, validade). Detalhes em [TEAM-KNOWLEDGE.md](TEAM-KNOWLEDGE.md).
- **IMAP somente leitura (T18).** O transporte de catálogo `local` (servidor embutido, sem `url` nem `launcher`) foi adicionado a `McpTransport`. Detalhes em [TEAM-IMAP.md](TEAM-IMAP.md).
- **Ativos monitorados (T23).** Novo `monitored_asset`: host que a empresa autoriza para diagnósticos de rede. Detalhes em [TEAM-DIAGNOSTICS.md](TEAM-DIAGNOSTICS.md).
- **Auditoria, retenção e backup (T30).** Sem contrato novo; a migração `0015` adiciona a exclusão controlada (`erasure_guard`, `erasure_log`). Detalhes em [TEAM-OPS.md](TEAM-OPS.md).
- **Registros financeiros (T24).** Novo `financial_record` (importação explícita, sem inferir valor ou moeda). Detalhes em [TEAM-FINANCE.md](TEAM-FINANCE.md).
- **Entrada de emails (T21).** Novos `mailbox_cursor` (cursor UIDVALIDITY/UID por pasta) e `email_message` (mensagem ingerida ou ignorada, com identificadores de thread). Detalhes em [TEAM-EMAIL-INTAKE.md](TEAM-EMAIL-INTAKE.md).
- **Agendamentos e notificações (T29).** Novos `schedule` (rotina opt-in que só enfileira jobs) e `notification` (alerta com contador). Detalhes em [TEAM-SCHEDULER.md](TEAM-SCHEDULER.md).
- **Saída por SMTP (T22).** `outbound_message` ganha campos opcionais `cc`, `bcc`, `attachments`, `message_id`, `in_reply_to`, `references`, `provider_result` e `simulated`. Detalhes em [TEAM-SMTP.md](TEAM-SMTP.md).
- **OAuth (T19).** Novos `oauth_app` (cliente OAuth registrado; segredo no cofre) e `oauth_grant` (metadados da concessão por conexão) e o tipo de credencial `oauth` (T20 acrescenta `directory`, o tenant permitido, em `oauth_app`). Detalhes em [TEAM-OAUTH.md](TEAM-OAUTH.md).
- **Lacunas de capacidade (T37).** Novo `capability_gap`: pedido que a equipe não soube atender, tentativas, tipo (`instrucao` ou `ferramenta`), assunto, demandas ligadas e a decisão do operador (`criar_skill`, `criar_ferramenta`, `nao_atender`); a recusa guarda uma `memory_item` confirmada. Detalhes em [TEAM-GAPS.md](TEAM-GAPS.md).
- **Pacotes.** `validate_bundle` confere unicidade de IDs, que cada referência (`ref:`) exista com o tipo certo e que nada cruze empresas.

## Compatibilidade e importação da configuração antiga

`config.json` e `config.example.json` **não mudam** e continuam sendo a única configuração lida pelo assistente atual. `team.legacy.import_legacy_config(config)` recebe o dicionário já carregado e devolve um rascunho validado (empresa "Local", departamento "Geral", um agente "Assistente" e uma conexão MCP por entrada de `mcp_servers`). Não lê nem grava arquivos; persistir o rascunho será trabalho de T02. URLs com credenciais são recusadas. Perfis de voz (`voice-settings.json`) e o cofre do Windows não são tocados; `voice_profile_ref` fica vazio até o operador escolher.

Mudanças futuras: incompatíveis incrementam `schema_version` e acrescentam um migrador; aditivas e opcionais mantêm a versão. Nenhum leitor aceita versão que não conhece.

## Limites

Planejamento de contratos, não funcionalidade para o usuário final: não há armazenamento, autorização em tempo de execução nem integração com agentes. A detecção de segredos é heurística e reduz, mas não elimina, o risco; o cofre e o gateway (T04, T07) continuam necessários. Os schemas JSON não cobrem regras entre campos (por exemplo `case_id` nulo apenas em `config.*`); use `validate`.
