# Auditoria, métricas, retenção e backup (T30)

Entrega da issue [#45](https://github.com/rafaelmaiabr/assistente/issues/45): `team/audit.py` (trilha e métricas), `team/retention.py` (exportação, exclusão e política), `team/backup.py` (backup e restauração), migração `0015`, `JobQueue.recover_all`. Usa persistência ([TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md)), fila ([TEAM-QUEUE.md](TEAM-QUEUE.md)), aprovações ([TEAM-APPROVALS.md](TEAM-APPROVALS.md)) e mensagens de saída ([TEAM-OUTBOUND.md](TEAM-OUTBOUND.md)).

Estado: serviços de backend testados, **sem tela e sem rotas HTTP** (a "central operacional" ainda não os expõe) e não ligados ao `TeamApp`; hoje são usados por código ou scripts do operador. Tudo exige o operador local confiável; agentes não leem a trilha nem apagam nada.

## Trilha de auditoria

`AuditService.trail(ctx, case_id=… | run_id=…)` devolve, em ordem cronológica, registros de: eventos da demanda (tipo, ator, revisão), execuções (agente, versão, estado, skills), decisões de ferramenta (ferramenta, operação, **decisão**, motivo, **política**, bytes e **duração**), jobs (estado, tentativas, **tokens** e passos conhecidos), aprovações (ação, estado, quem decidiu, hash do payload, valor/moeda), encaminhamentos, mensagens de saída (estado, aprovação, tentativas) e **referências de evidência** (id, fonte, tipo, confiabilidade, hash).

O que **nunca** entra: corpo de mensagens, resumos, argumentos e resultados de ferramentas, payload de jobs, tokens de acesso, raciocínio do modelo. Texto livre que entra (motivos, códigos de erro, fontes) passa por `sanitize_text`: redação de credenciais, e-mails, CPF/CNPJ, telefones, números de cartão, tokens longos, `Bearer …` e parâmetros de URL com `token=`/`senha=`, limitado a 200 caracteres.

**Payload bruto desativado por padrão.** O operador pode ligar `set_raw_payload(True)` (arquivo `audit.json`); mesmo assim só o payload dos **eventos** aparece, mascarado, e apenas quando a chamada pede `include_raw=True`.

## Métricas

`AuditService.metrics(ctx)` (por empresa, sem conteúdo): fila por estado, tentativas extras, esgotados, a reconciliar, idade do mais antigo pendente e eventos na caixa de saída; encaminhamentos por estado e os que exigem atenção; execuções por estado e decisões de ferramenta (permitidas/negadas/erro, duração média e máxima); falhas (execuções, ferramentas, mensagens falhas/desconhecidas, jobs falhos); **espera humana** (aprovações pendentes, a mais antiga, tempo médio de decisão, demandas por estado); **tokens** somados dos resultados dos jobs.

Tokens vêm do runtime: uso informado pelo provedor, ou estimativa pelo tamanho do texto quando ele não informa (o dado não distingue os dois). **Custo não é estimado**: `custo: null` com a observação de que preço e uso faturável não estão disponíveis; modelos locais não têm custo pago.

## Exportação

`RetentionService.export_case(ctx, case_id, acknowledge_sensitive=True)`: **sempre de uma única demanda** (não existe "exportar tudo"), só com o reconhecimento explícito de que contém dados pessoais. Grava `exports/<empresa>/<demanda>-<data>.json` (permissão 0600 onde o sistema respeita), com aviso no arquivo e na resposta, e registra no índice de exportações. Credenciais são removidas (`redact_secrets`; além disso os contratos já recusam segredos embutidos em demandas). A exportação **contém o conteúdo da demanda** (é o objetivo); guarde-a em local protegido.

## Exclusão e retenção

`erase_case(ctx, case_id, confirm=case_id)` apaga, **em uma única transação**, a demanda e tudo que depende dela: entradas, evidências, triagem, aprovações, encaminhamentos, mensagens de saída, jobs, comandos, subtarefas, execuções, decisões de ferramenta, eventos e suas linhas da caixa de saída. Depois remove as **exportações** da demanda. Fica apenas um registro sem conteúdo (`erasure_log`: ids, contagens, quando, por quem), somente de acréscimo. Se qualquer passo falhar, nada é apagado.

As tabelas "somente de acréscimo" (entradas, evidências, triagem) continuam recusando `DELETE`/`UPDATE` normais; a migração `0015` as deixa aceitar `DELETE` apenas enquanto a transação de exclusão mantém a linha de `erasure_guard`, que nunca sobrevive ao commit.

Política (`retention.json`, `set_policy`): `closed_case_days` (padrão: **vazio, nada é apagado automaticamente**), `export_days` (30) e `backup_days` (30). `apply_policy` apaga demandas **encerradas** (resolvida, cancelada, falhou) mais antigas que o limite e exportações vencidas; demandas abertas nunca. Não há agendador: o operador ou um script chama `apply_policy`/`prune`.

**Não alcança:** cópias em backups já feitos (expiram por idade, ver abaixo), memória e conhecimento que o operador criou a partir da demanda (T17), contatos, o texto que o SQLite pode manter em páginas livres até um `VACUUM`, e qualquer coisa fora deste computador.

## Backup e restauração

`BackupService.create(operator)`: cópia **consistente** pela API de backup do SQLite (pode ser feita com o app em uso) em `team-data/backups/<data>/`, com `manifest.json` (versão do esquema, SHA-256, tamanho, linhas por tabela, o que estava em andamento). Verifica a integridade da cópia. **Segredos não entram**: as senhas ficam no cofre do sistema (o banco só tem referências), então depois de restaurar em outro computador é preciso cadastrá-las de novo. **O backup não é criptografado** e contém dados de clientes: guarde em pasta que só você lê, não publique (`team-data/` e `*.sqlite3` já estão no `.gitignore`; se escolher outra pasta, proteja-a).

`restore(operator, pasta, overwrite=True)` (pare o aplicativo antes): `verify` confere manifesto, soma de verificação, `integrity_check`, `foreign_key_check` e se o backup não é de uma versão mais nova do app; guarda o banco atual como `.pre-restore-<data>`, troca o arquivo atomicamente, aplica migrações pendentes e **reconcilia**:

- jobs em execução voltam à fila, ou vão para `reconciliar` se um efeito externo estava em dúvida (`recover_all`);
- mensagens que estavam `enviando` viram `desconhecido` para decisão do operador;
- a caixa de saída pendente é mantida (consumidores deduplicam por evento).

**Nada é reenviado automaticamente.** `prune(operator, dias)` remove backups antigos (o mais recente é sempre mantido).

## Validação

`tests/test_team_ops.py` (17 testes): trilha por demanda e por execução com os campos exigidos; isolamento entre empresas; **canários** de segredo, e-mail, CPF e texto de corpo ausentes da trilha, das métricas e do que a exportação não deve levar; payload bruto desligado/ligado e mascarado; permissões; mascaramento; métricas de fila, falhas, espera humana e tokens sem custo; exportação com escopo e aviso; exclusão completa sem tocar em outra demanda, remoção das exportações, registro sem conteúdo e atomicidade (falha forçada reverte tudo); gatilhos somente de acréscimo preservados; política de retenção; backup com manifesto, adulteração/corrupção/versão futura recusadas; restauração com reconciliação de job com efeito em dúvida, job comum e mensagem em envio; restauração em local novo; poda.

## Limites

Sem interface, rotas ou agendamento; sem criptografia de backup ou de exportação; sem exclusão por contato/cliente (só por demanda); trilha sem busca textual; tokens podem ser estimativas; as métricas são instantâneas (sem série histórica). Não foi testado com um volume grande de dados nem com restauração entre computadores diferentes.
