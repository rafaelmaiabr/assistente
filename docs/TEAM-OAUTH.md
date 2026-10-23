# OAuth para conexão IMAP (T19: Gmail)

Entrega da issue [#34](https://github.com/rafaelmaiabr/assistente/issues/34): `team/oauth.py` (`OAuthService`, perfil `GOOGLE`), autenticação `XOAUTH2` no leitor IMAP, contratos `oauth_app` e `oauth_grant`, migração `0019`. Usa conexões e cofre ([TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md)) e o leitor IMAP ([TEAM-IMAP.md](TEAM-IMAP.md)). O motor é genérico: a T20 (Microsoft) acrescenta outro perfil.

Estado: validado **só com um provedor OAuth falso** e um servidor IMAP falso que aceita `XOAUTH2`. **Nenhuma conta real do Google foi usada.** O checklist real está abaixo e continua **pendente**. Não há tela nem ligação ao `TeamApp`; o operador usa o serviço (ou uma tela futura).

## O fluxo

1. **Antes de tudo (feito por uma pessoa, fora do sistema):** criar o aplicativo OAuth no provedor e anotar `client_id` (e o segredo, se houver). Este sistema **não cria projeto, aplicativo nem conta** no provedor. `register_app` guarda o `client_id` no banco e o segredo **só no cofre**.
2. `begin(conexão)` gera `state` (aleatório, uso único), `code_verifier`/`code_challenge` **PKCE S256** e `nonce`, abre um **ouvinte local de uma só vez** em `http://127.0.0.1:<porta aleatória>/oauth/callback` e devolve a URL de autorização para a pessoa abrir no navegador (com `access_type=offline` e `prompt=consent`, para o Google devolver o token de renovação).
3. O ouvinte só responde a `GET` nesse caminho exato e com o cabeçalho `Host` exato (contra rebinding), aceita só parâmetros conhecidos e únicos, **não reflete nada do que recebe**, compara o `state` em tempo constante, fecha após o primeiro resultado válido, ao cancelar, no prazo (3 min) ou depois de 5 requisições inválidas.
4. `complete(flow)` troca o `code` no endpoint de token (TLS verificado, sem redirecionamentos) com o verificador PKCE e o segredo do cofre. Exige o **token de renovação** e os **escopos obrigatórios**; confere `aud` e `nonce` da identidade; guarda o token de renovação **no cofre** (credencial de tipo `oauth`) e só metadados no banco (conta, escopos, estado, datas).

## Tokens

O **token de acesso** vive só na memória, com a validade. `access_token()` o renova sob demanda, **uma renovação por vez por conexão** (chamadas simultâneas compartilham o resultado), grava o novo token de renovação se o provedor o rotacionar e atualiza `renovada_em`. `invalid_grant` significa acesso revogado: a concessão vira `revogada`, a credencial também, e o conector recebe uma falha limpa até autorizar de novo. Indisponibilidade do provedor não muda nada (`provedor_indisponivel`). Nenhum resultado, erro, status ou documento contém token ou segredo (testado inspecionando o arquivo do banco e as respostas).

`ConnectionService` passou a aceitar resolvedores de segredo por tipo de credencial: para `oauth`, os conectores (IMAP) recebem o **token de acesso atual**, nunca o de renovação.

## Escopos e honestidade sobre o acesso

O Google só oferece, para IMAP, o escopo de acesso total à caixa (`https://mail.google.com/`). O `status` informa que **o token concedido é mais amplo do que as pastas autorizadas aqui**: o aplicativo lê somente as pastas permitidas e nunca altera mensagens, mas a permissão no Google é total até ser revogada. Não existe escopo somente leitura de IMAP no Google.

## Revogar, cancelar, reconectar

`disconnect` revoga no provedor (se falhar, avisa que é preciso remover o acesso na conta do provedor), apaga o token do cofre e marca a concessão `revogada`. `cancel` fecha o ouvinte (um retorno posterior é recusado). Autorizar de novo reativa a mesma credencial.

## IMAP com OAuth

`ImapConfig(auth="xoauth2")`: o leitor usa `AUTHENTICATE XOAUTH2` com o token de acesso; recusa vira `autenticacao_recusada` com a dica de reautorizar. O restante (pastas autorizadas, somente leitura, TLS) não muda.

## Configuração guiada (Google) — feita por uma pessoa

1. No Google Cloud Console, criar um projeto, configurar a tela de consentimento e criar um **ID do cliente OAuth do tipo "Aplicativo para computador"**. Copiar o `client_id` e o segredo.
2. Para uma conta pessoal ou de teste, deixar o aplicativo em modo de teste e adicionar o endereço como usuário de teste. **Limitações segundo a documentação do Google (confirme a versão atual):** em modo de teste, os tokens de renovação expiram em cerca de 7 dias e o número de usuários é limitado; o escopo `https://mail.google.com/` é "restrito" e publicar o aplicativo para o público exige verificação (e avaliação de segurança); contas do Google Workspace da própria organização podem usar o aplicativo como interno.
3. Garantir que o IMAP esteja habilitado na conta e usar o `client_id` em `register_app`.

## Checklist real (opcional, **pendente: não executado**)

Com uma conta de teste sua: registrar o aplicativo; `begin` → abrir a URL → conceder; `complete` → status `conectada` com a conta; `ImapAccounts.test` (conecta a `imap.gmail.com` por `XOAUTH2`); revogar o acesso na conta Google e conferir que o próximo uso pede nova autorização; `disconnect`. Registrar o resultado (sucesso ou pendência) sem copiar tokens. Nada disso foi feito nesta entrega; o provedor real pode exigir ajustes (por exemplo, parâmetros do consentimento).

## Validação

`tests/test_team_oauth.py` (17 testes) com `tests/oauth_fake.py` (servidor OAuth com PKCE obrigatório, redirecionamento só em loopback, rotação e revogação) e o IMAP falso com `XOAUTH2`: fluxo completo e tokens fora do banco; leitura da caixa por XOAUTH2; `state` errado, ausente ou adulterado, caminho e `Host` errados, parâmetros extras ou repetidos; código adulterado; retorno repetido; excesso de requisições inválidas; consentimento negado, prazo esgotado e cancelamento; sem token de renovação e escopo insuficiente (nada gravado); token expirado com **oito chamadas simultâneas gerando uma única renovação**; rotação; revogação no provedor (reautorização necessária); indisponibilidade do provedor; desconexão com e sem o provedor; permissões, isolamento entre empresas, HTTPS obrigatório e erros do IMAP sem eco de token.

## Limites

Só o perfil do Google existe (T20: Microsoft); sem tela; sem `device flow`; o fluxo depende de o computador abrir um navegador e de a porta de loopback estar livre; a identidade (`id_token`) vem do endpoint de token via TLS e não tem a assinatura revalidada; o perfil do Google segue a documentação pública e **não foi exercitado contra o serviço real**.
