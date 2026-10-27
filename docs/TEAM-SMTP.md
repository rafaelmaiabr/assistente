# Saída de emails por SMTP com aprovação e reconciliação (T22)

> **Camada de conectores:** este servidor/transporte é o driver `smtp` da capacidade `email-envio` (sem mudança de comportamento); ver [TEAM-CONNECTORS-EMAIL.md](TEAM-CONNECTORS-EMAIL.md).

Entrega da issue [#37](https://github.com/rafaelmaiabr/assistente/issues/37): `team/smtp_transport.py` (SMTP real, simulado, roteador e configuração), `team/attachments.py` (anexos locais), ampliação de `team/outbound.py` (Cc/Cco, anexos, threading, Message-ID próprio, resultado técnico). Sobre a comunicação externa da T14 ([TEAM-OUTBOUND.md](TEAM-OUTBOUND.md)), aprovações ([TEAM-APPROVALS.md](TEAM-APPROVALS.md)) e entrada de emails ([TEAM-EMAIL-INTAKE.md](TEAM-EMAIL-INTAKE.md)).

Estado: validado com um **servidor SMTP falso**, inclusive TLS (`STARTTLS` com certificado autoassinado gerado com `openssl`). **Nenhum email real foi enviado.** O teste real exige uma conta e um destinatário autorizados, e só deve ser feito com os dois (roteiro abaixo). Não há tela nem ligação ao `TeamApp`. OAuth (T19/T20) não existe: a autenticação é usuário + senha de aplicativo no cofre.

## IMAP não envia; SMTP só com configuração explícita

Uma conta de saída de email é uma conexão do tipo `smtp`; uma conexão `imap` é recusada como conta de saída. O operador a vincula a um servidor com `SmtpAccounts.configure` (host, porta, usuário, remetente, `starttls` ou `ssl`), e a senha fica no cofre. **TLS é obrigatório e verificado** (certificado e nome); sem TLS só em loopback para testes. `SmtpAccounts.test` conecta, negocia TLS e autentica, **sem enviar mensagem**, com erros acionáveis (`autenticacao_recusada`, `tls_invalido`, `sem_conexao`) que nunca contêm a senha.

## Adaptador simulado por padrão

`RoutingEmailTransport` usa o SMTP real **somente** para conexões configuradas; qualquer outra vai para `FakeEmailTransport`, que não toca a rede. Resultado simulado fica marcado (`simulated: true`, resultado "envio simulado; nada saiu do computador") e a entrada de histórico começa com "[ENVIO SIMULADO - nada saiu do computador]". Envio real exige: conexão ativa, configuração SMTP, aprovação atual do payload exato e as demais verificações da T14.

## O que a aprovação cobre

O hash do rascunho, os parâmetros mostrados ao aprovador e a política de destinatários agora incluem **Para, Cc, Cco, anexos (nome, tamanho, SHA-256), Message-ID próprio, In-Reply-To e References**. O aprovador vê todos os destinatários, inclusive Cco. Alterar qualquer parte depois da aprovação muda o hash e o envio é bloqueado (`rascunho_alterado`) antes de qualquer conexão. Cc/Cco seguem a mesma política de destinatários (contato vinculado ou domínio autorizado); no máximo 5 endereços no total, sem repetição entre as listas; Cc/Cco só existem no email. Assunto é reduzido a uma linha (sem injeção de cabeçalhos).

## Anexos

`AttachmentStore`: até 3 arquivos, 2 MB cada e 5 MB no total; lista de tipos permitidos (`.pdf`, `.png`, `.jpg`, `.jpeg`, `.txt`, `.csv`), assinatura do conteúdo conferida para PDF/PNG/JPEG e UTF-8 para texto; nunca executáveis, arquivos compactados ou Office; nome reduzido a um nome seguro (sem caminho). Só os metadados vão para a mensagem; os bytes ficam em `attachments/<empresa>/<demanda>/<sha256>` e são **relidos e conferidos contra o hash aprovado** no momento do envio (arquivo trocado → não enviado). Excluir a demanda (T30) apaga também seus anexos.

## Threading e Message-ID próprio

Ao criar o rascunho de uma demanda que veio de email (T21), `In-Reply-To` e `References` saem da mensagem recebida mais recente. O `Message-ID` próprio (`<uuid@domínio-do-remetente>`) é gerado na criação do rascunho, entra no hash aprovado, é enviado no cabeçalho e fica gravado como `message_id` e `provider_id`. Todo email enviado leva `X-Assistente-Origem`, que a ingestão da T21 reconhece para não criar laços.

## Resultado, desconhecido e reconciliação

O diálogo SMTP é feito passo a passo:

- Recusa **antes de `DATA`** (conexão, TLS, login, remetente, qualquer destinatário) ou resposta diferente de `250` ao `DATA`: **não enviada** (`falhou`, `recusada_pelo_provedor:<motivo>`), definitiva. Se um destinatário é recusado, a mensagem inteira é abandonada.
- `250` ao `DATA`: **aceita pelo servidor**, nunca "entregue" ou "lida"; guarda o resultado técnico (`provider_result`).
- Timeout ou queda **depois de iniciar o `DATA`**: estado `desconhecido`, **sem nenhuma nova tentativa automática**; fica na fila de reconciliação do operador, que confere a pasta Enviados pelo Message-ID e registra `enviada` (com o id) ou `nao_enviada`. SMTP não tem consulta (`lookup`), então não há reconciliação automática.
- Credencial revogada ou conta inativa: nenhuma conexão é aberta (`credencial_ou_conta_indisponivel`).
- Troca de dono depois da aprovação: bloqueada. Reenvio do mesmo rascunho: o segundo despacho encontra a mensagem já enviada e não abre outra conexão.

## Teste real (manual, com destinatário autorizado)

1. Crie uma conexão `smtp` com senha de aplicativo, configure o servidor (`SmtpAccounts.configure`) e rode `SmtpAccounts.test`.
2. Vincule como contato **um endereço seu** (ou autorize seu domínio) e crie uma demanda de teste.
3. Crie o rascunho para esse endereço, peça a aprovação, aprove no painel e despache.
4. Confira: a mensagem chegou, o `Message-ID` bate com o registrado, e o estado ficou `enviada` com o resultado técnico. Nada disso foi executado nesta entrega.

## Validação

`tests/test_team_smtp.py` (18 testes, servidor SMTP falso): envio com Cc/Cco (Cco ausente dos cabeçalhos), Message-ID persistido, remetente e usuário; threading; anexos válidos e inválidos (executável, assinatura falsa, vazio, grande, quantidade, texto não UTF-8, zip, nome inválido); payload aprovado cobrindo Cco/anexos (adulteração bloqueia sem conexão) e anexo trocado; política e limites de Cc/Cco; resposta perdida após `DATA` e timeout (desconhecido, sem retentativa, reconciliação); recusa de destinatário e de `DATA`; senha errada e credencial revogada; envio duplicado e troca de dono; aceitação ≠ entrega; adaptador simulado por padrão; IMAP não é conta de saída; permissões e validação da configuração; teste de conexão; TLS (`STARTTLS`) com certificado não confiável e com CA confiável; exclusão de demanda removendo anexos. O teste achou um defeito real: `smtplib` devolve em vez de lançar quando o `DATA` recebe 5xx, e isso agora é tratado como recusa.

## Limites

Sem OAuth, sem DKIM/SPF próprios (dependem do servidor), sem HTML (só texto simples), sem anexos grandes ou em outros formatos, sem consulta à pasta Enviados para reconciliar sozinho, sem confirmação de leitura ou entrega, sem tela. Validado apenas contra servidor falso.
