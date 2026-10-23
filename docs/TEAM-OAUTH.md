# OAuth para conexão IMAP (T19: Gmail; T20: Microsoft 365 e Outlook)

Entrega da issue [#34](https://github.com/rafaelmaiabr/assistente/issues/34): `team/oauth.py` (`OAuthService`, perfil `GOOGLE`), autenticação `XOAUTH2` no leitor IMAP, contratos `oauth_app` e `oauth_grant`, migração `0019`. Usa conexões e cofre ([TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md)) e o leitor IMAP ([TEAM-IMAP.md](TEAM-IMAP.md)). O motor é genérico; a T20 acrescenta o perfil da Microsoft e o fluxo por código de dispositivo (seção "Microsoft" abaixo).

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

---

# Microsoft 365 e Outlook (T20)

Entrega da issue [#35](https://github.com/rafaelmaiabr/assistente/issues/35): perfil `MICROSOFT`, **fluxo por código de dispositivo** (`begin_device` / `complete_device`), escolha obrigatória do diretório permitido, diagnóstico dos erros da Microsoft e campo `directory` em `oauth_app`. Reaproveita todo o resto da T19 (cofre, renovação em fluxo único, XOAUTH2, concessão e estados).

Estado: validado **só com um provedor Microsoft falso** e o IMAP falso. **Nenhuma conta ou tenant real foi usado**; o roteiro real é opcional e continua **pendente**.

## Por que código de dispositivo

A Microsoft só aceita para aplicativos locais um redirecionamento `http://localhost`, e nem toda configuração aceita o endereço de loopback que o nosso ouvinte usa. O fluxo por código de dispositivo dispensa porta local e redirecionamento: `begin_device` pede um código ao provedor, mostra o endereço oficial e o código à pessoa, e `complete_device` consulta o provedor no intervalo indicado (respeitando `slow_down`) até a pessoa aprovar, recusar, o código expirar (15 min) ou o fluxo ser cancelado. Só o operador inicia; a mensagem avisa para conferir o endereço oficial e nunca digitar um código que outra pessoa enviou (golpe do código de dispositivo). O fluxo de redirecionamento do navegador não é oferecido para este provedor (`fluxo_nao_suportado`).

## Diretório (tenant) permitido

`register_app(..., tenant=...)` é **obrigatório** para a Microsoft: o GUID ou domínio do diretório, ou `organizations` / `consumers` / `common`. O diretório vai em todas as URLs e, depois do login, a identidade devolvida é conferida: com um GUID, o `tid` precisa ser igual; `organizations` recusa contas pessoais; `consumers` exige conta pessoal. Conta de outro diretório é recusada (`tenant_nao_permitido`) e **nada é guardado**. Recomenda-se informar o GUID da organização.

## Permissões, consentimento e limites

Escopos pedidos: `https://outlook.office.com/IMAP.AccessAsUser.All`, `offline_access`, `openid`, `email`, `profile`. A permissão de IMAP dá acesso à caixa inteira do usuário (inclusive para alterar); o aplicativo só lê as pastas autorizadas, mas o status diz que a permissão é mais ampla. **Consentimento de organização:** muitas organizações exigem que um administrador do Microsoft 365 consinta; a mensagem de erro correspondente (`AADSTS65001`/`90094`) explica isso. O sistema nunca pede a senha principal nem sugere desativar segurança.

## Diagnóstico

Os códigos `AADSTS` conhecidos viram mensagens acionáveis (consentimento necessário ou do administrador, aplicativo ou diretório inexistente, conta de outro diretório, fluxo de cliente público desativado, escopo inválido, Exchange Online indisponível, verificação em duas etapas, bloqueio por acesso condicional, senha alterada/acesso expirado → reautorizar). Apenas o número do código aparece; o texto técnico do provedor nunca é repassado. **IMAP desativado na caixa** ou token recusado pelo servidor de email aparece como `autenticacao_recusada` com a dica "IMAP desativado na conta … autorize a conexão de novo".

## Tokens, renovação e saída

Igual à T19: token de renovação só no cofre, de acesso só na memória, uma renovação por vez, `invalid_grant`/`interaction_required` → `revogada` e reautorização. **A Microsoft não oferece revogação do token por este caminho:** `disconnect` apaga o token local, marca a concessão `revogada` e avisa que o acesso continua concedido até ser removido em `https://myapps.microsoft.com` (ou pelo administrador).

## Configuração guiada (Microsoft Entra) — feita por uma pessoa

1. Em **Microsoft Entra ID → Registros de aplicativo**, criar um aplicativo (preferencialmente de **locatário único**), anotar o ID do aplicativo (cliente) e o **ID do diretório (locatário)**.
2. Em **Autenticação**, ativar **Permitir fluxos de cliente público** (necessário para o código de dispositivo; sem isso aparece `AADSTS7000218`).
3. Em **Permissões de API**, adicionar a permissão delegada **IMAP.AccessAsUser.All** (Office 365 Exchange Online) e `offline_access`; se a organização exigir, pedir o **consentimento do administrador**.
4. No Exchange Online, o IMAP precisa estar habilitado na caixa (por exemplo `Set-CASMailbox -ImapEnabled $true`).
5. Usar o ID do aplicativo e o locatário em `register_app`; o servidor IMAP é `outlook.office365.com:993` com `auth="xoauth2"`.

*Segundo a documentação pública da Microsoft na data da escrita; confirme a versão atual — esta entrega não foi exercitada contra o serviço real. Contas pessoais (Outlook.com) usam `consumers` e têm regras próprias que não foram verificadas aqui.*

## Checklist real (opcional, **pendente: não executado**)

Com um tenant de teste ou conta própria: registrar o aplicativo; `begin_device` → abrir o endereço e digitar o código → `complete_device`; `ImapAccounts.test` contra `outlook.office365.com`; recusar o consentimento e conferir a mensagem; remover o acesso em myapps.microsoft.com e conferir o pedido de nova autorização; `disconnect`. Registrar sucesso ou pendência sem copiar tokens.

## Validação

`tests/test_team_oauth_microsoft.py` (12 testes) com `FakeMicrosoft` (tenant no caminho, cliente público, código de dispositivo, erros `AADSTS`): fluxo completo e leitura da caixa por XOAUTH2; espera, `slow_down` e intervalo; recusa, expiração, cancelamento e código desconhecido sem nada guardado; conta de outro diretório; `organizations` x `consumers`; diretório obrigatório e validado; fluxo de redirecionamento não oferecido; seis erros de consentimento/configuração sem eco do texto do provedor; diretório ou aplicativo inexistentes; IMAP recusando o token; renovação, reautorização por `AADSTS50173` e logout com o aviso de revogação; nenhum token em resultados ou no banco.

## Limites (T20)

Só código de dispositivo; sem tela; Google e Microsoft são os únicos perfis; o `tid` vem do `id_token` recebido do endpoint de token via TLS (assinatura não revalidada); configurações corporativas (acesso condicional, MFA obrigatório, IMAP desativado) só aparecem como diagnóstico, nunca contornadas.
