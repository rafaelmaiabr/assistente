# Checklist de release da equipe de agentes

Use antes de publicar uma versão e depois de mudar prompt, skill, modelo, esquema do banco ou regra de segurança. Marque cada item no PR da release; o que não foi feito é **dito**, não omitido. Origem: [TEAM-EVALS.md](TEAM-EVALS.md) (T33).

## 1. Antes de gerar a versão

- [ ] Suíte Python completa: `python -m unittest discover -s tests` (inclui os testes dos evals).
- [ ] **Evals offline:** `python scripts/run_evals.py` termina com `Resultado: OK` (zero violações de gate e nenhuma regressão). Em caso de falha, anexe `--markdown` ao PR e **não** atualize a base para "fazer passar": entenda a causa.
- [ ] Se o corpus ou o comportamento medido mudou de propósito: `python scripts/run_evals.py --update-baseline`, revisão do diff de `evals/baseline.json` e justificativa no PR.
- [ ] Interface e desktop: `npm test` e `npm run build` em `frontend/`, `node --test desktop/tests/*.test.cjs`, `node --check` de `desktop/main.cjs` e `desktop/preload.cjs`; se tocou no desktop, `electron . --smoke-test`.
- [ ] `python -m team.doctor` sem erros em uma instalação nova e em uma atualizada de uma versão anterior (migrações, dependências, pasta de dados, cofre, modelo).
- [ ] Pacote Windows: `npm test --prefix desktop` (o inventário recusa arquivos privados ou inesperados) e, ao gerar o pacote, conferir o `build-manifest.json` (`containsLocalConfig` e `containsUserData` falsos; lista de migrações igual à de `team/migrations/`).
- [ ] Esquemas exportados em dia (`python scripts/export_team_schemas.py`) e Graphify atualizado pelo wrapper (`scripts/graphify.ps1`).
- [ ] Mudou prompt, skill ou modelo? Rode também, se houver Ollama local, `python scripts/run_evals.py --ollama` e compare latência, tokens e acertos com a versão anterior (opcional, não é gate).
- [ ] Nenhum dado real no repositório: sem `config.json`, logs, conversas, áudio, bancos, backups, modelos ou credenciais.

## 2. Migração e reversão

- [ ] Liste as migrações novas (`team/migrations/`). Cada uma roda em transação e é registrada com soma de verificação; uma migração já aplicada **não** pode ser editada.
- [ ] Antes de migrar um banco existente o aplicativo guarda `team.sqlite3.pre-migracao-NNNN.bak` ao lado dele ([TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md)). Confirme que a cópia foi criada em um banco de teste atualizado de uma versão anterior.
- [ ] Faça um **backup verificado** ([TEAM-OPS.md](TEAM-OPS.md)) antes de atualizar uma instalação real: `BackupService.create` e `verify`.
- [ ] Reversão: pare o aplicativo, restaure o `.pre-migracao-NNNN.bak` (ou o backup) e volte à versão anterior do programa; um banco de versão mais nova **é recusado** pela versão antiga, em vez de ser alterado. Segredos não voltam com o backup (ficam no cofre do sistema).

## 3. Parar o que está em andamento

- [ ] Parar o backend (`scripts/stop-web.ps1` ou fechar o aplicativo). Trabalhos pendentes continuam no banco e são retomados na próxima partida (`recover_all`); efeitos externos com resultado incerto ficam em reconciliação e **não** são reenviados sozinhos.
- [ ] Se algum worker foi iniciado à parte (o aplicativo não inicia workers por conta própria), pare-o antes de migrar.
- [ ] Demandas que não devem continuar: pausar, assumir ou rejeitar pela central de demandas (pausa os trabalhos da demanda).
- [ ] Acesso remoto ligado (`remote.enabled`)? Desligue-o em `config.json` e reinicie, ou revogue as sessões: o dono local pode revogar as de uma pessoa ou desativá-la ([TEAM-REMOTE.md](TEAM-REMOTE.md)).

## 4. Revogar acessos se algo der errado

- [ ] **Skills:** desativar a skill no painel (concessões já feitas não são ampliadas por nova versão); remover concessões do agente.
- [ ] **Conexões:** marcar a conexão como inativa e remover a credencial (o segredo sai do cofre); no OAuth, desconectar a concessão (o token de renovação é revogado no provedor quando possível).
- [ ] **Pessoas e sessões:** desativar a pessoa (encerra todas as sessões na hora) ou remover o acesso da empresa.
- [ ] **Saída de mensagens:** aprovações pendentes são revogadas ao assumir a demanda; mensagens em rascunho podem ser canceladas.

## 5. Pendências com serviços reais (relate sempre)

Os testes automáticos usam provedores falsos. Para cada item abaixo, escreva no PR se foi validado com o serviço real, por quem e quando, ou "não validado":

- [ ] Login com **Google** de verdade (cadastrar o email, entrar, conferir papéis e escopo) e a implantação atrás de proxy com TLS (por exemplo Tailscale).
- [ ] **OAuth** Gmail e Microsoft 365 para IMAP; leitura real de uma caixa de entrada.
- [ ] **SMTP** real (envio de uma mensagem de teste a um destinatário próprio, com aprovação).
- [ ] **Registrador** e **ERP/financeiro**: seguem sob demanda (#41 e #42); nenhum conector real existe.
- [ ] **Ollama** real: smoke do modelo configurado e, se desejado, `--ollama`.
- [ ] Teste manual com microfone e voz reais no aplicativo.

## 6. Depois de publicar

- [ ] Atualizar o acompanhamento #15 (checklist) somente após o merge validado.
- [ ] Registrar limitações novas nos documentos da área, nunca declarar integração pronta sem a validação real do item 5.
