# Persistência da equipe (T02)

Entrega da issue [#17](https://github.com/rafaelmaiabr/assistente/issues/17): armazenamento local SQLite para os contratos de [TEAM-CONTRACTS.md](TEAM-CONTRACTS.md). Ainda não há fila, workers nem painel; o backend de chat não grava demandas.

## Onde fica

`<pasta de dados>/team-data/team.sqlite3` (a pasta de dados é `ASSISTENTE_DATA_DIR`, ou a raiz do projeto em desenvolvimento). Fica fora do Git (`.gitignore`, `.graphifyignore`) e fora do pacote Windows, que só leva o código de `team/` e as migrações. O estado do interruptor está em `team-data/persistence.json`.

## Atendimento persistente é opcional

- Padrão: **desligado**. Nada é criado em disco e o chat segue efêmero (histórico descartado ao atualizar a página).
- Na interface, o menu **Atendimento persistente** mostra o aviso e ativa/desativa. API local (mesma origem): `GET /api/persistence`, `POST /api/persistence/enable`, `POST /api/persistence/disable`.
- Ativar migra o banco antes de ligar o interruptor. Se a migração falhar, continua desligado e o erro é mostrado. **Conversas anteriores não são importadas** e desativar nunca apaga o banco.

## Migrações

Arquivos `team/migrations/NNNN_nome.sql`, numerados sem lacunas, aplicados em ordem, cada um em uma transação (DDL incluída) com checksum registrado em `schema_migrations`.

- Falha: a migração é revertida, o arquivo do banco é preservado e a mensagem diz qual migração falhou. Não há reset ou recriação automática.
- Antes de aplicar migrações pendentes a um banco existente, é feita uma cópia `team.sqlite3.pre-migracao-NNNN.bak` ao lado dele.
- Banco com migração desconhecida (aplicativo mais antigo) ou com migração editada depois de aplicada é recusado sem alterações. Arquivo que não é SQLite também é recusado sem ser tocado.
- Para recuperar manualmente: feche o aplicativo e restaure a cópia `.bak`.

## Repositório

`team.repository.Repository` / `TenantSession` definem a fronteira (PostgreSQL virá em T34); `SqliteRepository` a implementa.

- Tudo acontece em `with repo.transaction(tenant_id) as session`. O contexto da empresa é obrigatório e a empresa precisa existir. Leituras e escritas só enxergam essa empresa; documento de outra empresa gera `TenantMismatch`.
- Chaves estrangeiras compostas `(tenant_id, id)` impedem referenciar departamento, agente, demanda etc. de outra empresa (`IntegrityViolation`).
- `session.save(doc)` valida o contrato (T01) e controla revisão: novo documento tem revisão 1; atualização exige revisão armazenada + 1, senão `RevisionConflict`.
- `session.save(doc, event=evento)` grava estado, evento e linha de outbox **na mesma transação**; qualquer exceção desfaz tudo. Uma revisão de evento por demanda é única.
- `register_command` armazena um comando uma vez por chave de idempotência (devolve `False` na repetição).
- Escritores usam `BEGIN IMMEDIATE` (um por vez, os demais aguardam até 5 s); WAL não é usado, para manter o arquivo único.
- `pending_outbox` / `mark_dispatched` expõem a outbox; claim por lease, heartbeat e retry são da T10.

## Limites

Sem criptografia do banco (proteger a pasta de dados do usuário), sem retenção/backup automático (T30) e sem importação de conversas. Apenas `enable`/`disable` na interface; criação de empresas, agentes e demandas pela UI vem em T15/T16. Validado com testes automatizados em banco temporário.
