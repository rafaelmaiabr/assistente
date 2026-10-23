# Entrada de emails como demandas (T21)

Entrega da issue [#36](https://github.com/rafaelmaiabr/assistente/issues/36): `team/email_ingest.py` (`EmailIngest`), contratos `mailbox_cursor` e `email_message`, migração `0017`, e pequenas extensões em `team/imap_client.py` (cabeçalhos estruturados, `uids_after`, `validity`) e `CaseService` (variantes `*_in` que rodam na transação de quem chama). Usa o leitor IMAP somente leitura ([TEAM-IMAP.md](TEAM-IMAP.md)), demandas ([TEAM-CASES.md](TEAM-CASES.md)) e retenção ([TEAM-OPS.md](TEAM-OPS.md)).

Estado: ingestão **manual** (o operador chama `ingest`; nada roda em segundo plano), sem tela e sem ligação ao `TeamApp`. A saída de emails (SMTP, T22) e o OAuth (T19/T20) não existem aqui. Validada com um servidor IMAP falso; não foi testada com uma caixa real.

## Caixa, pasta e departamento

`bind_mailbox(ctx, connection_id, folder, department_id)` (só o operador): vincula **uma pasta autorizada** de uma conexão `imap` já configurada ([TEAM-IMAP.md](TEAM-IMAP.md)) ao departamento que receberá as demandas (normalmente a Secretaria). Pasta fora da lista autorizada é recusada. O vínculo guarda o cursor `(UIDVALIDITY, último UID)`.

## Ingestão

`ingest(ctx, connection_id, folder, limit=20)`: lê, em ordem, as mensagens com UID maior que o cursor (máx. 50 por chamada) usando só `EXAMINE`/`BODY.PEEK` (nada é marcado como lido), com a senha do cofre. Cada mensagem é processada em **uma única transação**: demanda (ou entrada na demanda existente), entrada de histórico, evidência de proveniência, registro da mensagem e avanço do cursor. Se algo falhar no meio, a mensagem inteira é revertida, o cursor não anda, o relatório informa `parou_em` e a próxima chamada recomeça nela sem duplicar nada.

- **Reimportação e UIDVALIDITY**: uma UIDVALIDITY nova zera o cursor de UID (`uidvalidity_reiniciada`), mas cada mensagem também é deduplicada por `Message-ID` por conexão, então o reenvio dos mesmos emails com UIDs novos vira `duplicada` e não abre demandas. Dois emails sem `Message-ID` não são deduplicados por esse critério (só pelo UID).
- **Demanda criada**: estado `recebida`, origem `email`, `customer_ref = email:<remetente>`, objetivo `E-mail: <assunto>`, sem responsável. A triagem da Secretaria (T12) segue o fluxo normal a partir daí; esta entrega não dispara triagem sozinha.
- **Texto**: remetente/destinatários/assunto no cabeçalho do registro, corpo limitado a 8 000 caracteres, HTML reduzido a texto sem carregar nada, anexos apenas listados (tipos) na evidência. Strings com cara de segredo (e a senha da própria caixa, se aparecer no email) são mascaradas. Texto que parece instruir o assistente é sinalizado na evidência e continua sendo só dado.

## Threads sem confiar no assunto

Uma resposta só entra numa demanda existente quando `In-Reply-To`/`References` apontam para uma mensagem já ingerida **e** o remetente já participava daquela conversa (remetente ou destinatário de alguma mensagem dela). O assunto nunca junta conversas (dois clientes escrevendo "Fatura" geram duas demandas). Casos duvidosos abrem demanda nova marcada para revisão: `resposta_nao_confirmada` quando o remetente está fora da conversa (cabeçalhos forjados) ou as referências apontam para várias demandas; resposta a pai desconhecido vira `nova_demanda`. Resposta em demanda já encerrada é anexada ao histórico, mas **não reabre** a demanda automaticamente.

## Identidade do remetente

O remetente é **identidade não verificada**: não há checagem de SPF/DKIM/DMARC nesta entrega. Se o endereço pertence a um contato **vinculado**, o registro marca `contato_vinculado`, o que ainda não é autenticação. As políticas de destinatários e de aprovação (T13/T14) continuam sendo o que impede revelar dados ou cobrar com base em um email recebido.

## Laços e respostas automáticas (regra explícita)

Ficam registradas como `ignorada_loop` ou `ignorada_autoresposta`, sem abrir demanda, mas avançam o cursor: remetente igual à própria caixa; cabeçalho `X-Assistente-Origem` (marca que a T22 deverá pôr nos emails que enviar); `Auto-Submitted` diferente de `no`; `Precedence` bulk/junk/list/auto_reply; `X-Autoreply`, `X-Autorespond`, `X-Auto-Response-Suppress`; `List-Id`; `Return-Path` vazio; remetentes `postmaster`/`mailer-daemon`; assuntos típicos de ausência e de falha de entrega. A lista é a de `ignore_reason`; remetentes de notificações legítimas como `noreply@` **não** são ignorados por padrão.

## Rastreio até a fonte

`sources_for_case(ctx, case_id)` devolve pasta, UIDVALIDITY, UID, `Message-ID`, `In-Reply-To`, classificação e identidade de cada mensagem da demanda, sem texto. A evidência da demanda tem a fonte `email:<pasta>/<uidvalidity>/<uid>`. Excluir uma demanda (T30) apaga também esses registros; depois disso a deduplicação por `Message-ID` daquela mensagem se perde, e um reenvio dela abriria nova demanda.

## Validação

`tests/test_team_email_ingest.py` (13 testes, servidor IMAP falso): demandas com identidade não verificada e proveniência, contato vinculado, execuções incrementais, reimportação, mudança de UIDVALIDITY sem duplicar, mesmo assunto de clientes diferentes, resposta legítima por cabeçalhos (inclusive com assunto diferente), cabeçalhos forjados, referências a várias demandas e a pai desconhecido, oito tipos de auto-resposta/laço, HTML hostil, anexo, segredo e senha da caixa mascarados, falha no meio do lote e retomada, permissões, pasta não autorizada, erro de login sem expor a senha, e exclusão de demanda removendo os registros.

## Limites

Sem polling, SMTP, OAuth, SPF/DKIM, reabertura automática de demandas, leitura de anexos, nem tratamento de emails sem `Message-ID` além do UID. Uma pasta por vínculo e no máximo 50 mensagens por chamada. Não testado com provedores reais.
