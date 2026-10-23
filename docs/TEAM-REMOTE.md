# Acesso remoto com login Google, papéis por empresa e isolamento (T32, parte 1)

Primeira parte da issue [#47](https://github.com/rafaelmaiabr/assistente/issues/47), que foi subdividida antes de codificar: aqui estão o login com Google, as sessões, os papéis por empresa, o listener remoto separado, CSRF/origens/limites e a suíte de isolamento. O **escopo por departamento** e a **identidade própria de worker** ficam em [#84](https://github.com/rafaelmaiabr/assistente/issues/84); #47 só fecha com a #84.

Código: `team/identity.py` (pessoas, papéis, sessões, trilha), `team/login.py` (configuração e fluxo do Google), `team/remote.py` (aplicação remota, regras por rota), `team/access.py` (`member_context`, `is_operator`), `scripts/remote_setup.py` e a migração `0021_identidade.sql`. Na interface: tela de login, barra com a pessoa, aba **Membros**.

## Dois modos, que não se misturam

| | Local (como sempre) | Remoto (novo, opcional) |
|---|---|---|
| Endereço | `127.0.0.1:8765` | porta própria (padrão `8766`), em loopback por padrão |
| Quem é | o **dono local**, sem login | **pessoas** que entram com Google |
| O que existe | chat, voz, ligar/desligar a persistência, Equipe | **só** a interface, `/auth/*` e a API `/api/team` por empresa |
| Dono local | sim | **não existe**: nenhuma sessão vira dono, e o dono local não é administrador global do ambiente remoto |

O listener remoto só sobe com `remote.enabled: true` no `config.json`. Ele **não é iniciado por padrão nem pelo pacote portátil**, e escutar fora do loopback exige TLS direto (certificado e chave) ou um proxy com TLS à frente. O WebSocket do chat, as vozes e a chave da persistência **não são registrados** no listener remoto: nenhuma sessão os alcança. (Consequência: a conversa vinculada a uma demanda, do T31, funciona só no modo local.)

## Configuração

Em `config.json` (nunca vai para o Git):

```json
"remote": {
  "enabled": true,
  "public_origin": "https://meu-pc.minha-rede.ts.net",
  "google_client_id": "XXXX.apps.googleusercontent.com",
  "allowed_domains": [],
  "bind": "127.0.0.1",
  "port": 8766
}
```

Campos opcionais: `allowed_domains` (restringe a domínios do Google Workspace, pelo campo `hd`), `idle_minutes` (padrão 480), `session_hours` (padrão 168), `tls_cert`/`tls_key` (TLS direto; obrigatórios se `bind` não for loopback), `trust_forwarded_for` (só para um proxy local; veja abaixo). Campos desconhecidos, origem sem https (exceto `localhost`), caminho na origem, porta baixa e bind público sem TLS são **recusados** na partida, com mensagem; nesse caso só o modo local sobe.

Passos:

1. No Google Cloud, crie um **cliente OAuth do tipo "Aplicativo da Web"** e registre como URI de redirecionamento o endereço mostrado por `python scripts/remote_setup.py status` (`<public_origin>/auth/google/callback`). Os escopos pedidos são só `openid email profile`.
2. Grave o segredo do cliente no cofre do Windows: `python scripts/remote_setup.py set-secret` (pede sem eco; não vai para o `config.json`, nem para logs ou respostas). `clear-secret` remove.
3. Ative a persistência, abra a **Equipe** no modo local, escolha a empresa, aba **Membros**, e cadastre o email do primeiro **administrador**. Cadastrar não dá acesso: a pessoa precisa entrar com essa conta Google. A partir daí os administradores da empresa gerenciam os próprios membros.
4. Exponha o listener. A opção recomendada (**não validada nesta entrega**) é manter `bind` em `127.0.0.1` e usar um proxy com TLS e rede privada, por exemplo `tailscale serve` apontando para `http://127.0.0.1:8766`, com `public_origin` igual ao nome HTTPS que o proxy publica. O `Host` da requisição precisa ser exatamente o de `public_origin`.

## Como o login funciona

Fluxo *authorization code* com PKCE (S256) do Google:

- `GET /auth/google/start` guarda no servidor um `state` de uso único (10 min), um `nonce` e o verificador PKCE, e põe no navegador um cookie curto (`HttpOnly`, `SameSite=Lax`) cujo hash fica ligado ao `state`. O callback só vale se o `state` for conhecido, não usado, não expirado **e** o cookie for o mesmo: um link montado por outra pessoa não loga ninguém na sessão de um invasor, e um `state` roubado não serve em outro navegador. O `state` é queimado mesmo quando a verificação falha.
- `GET /auth/google/callback` troca o código no endpoint do Google por TLS verificado, com o segredo do cofre no corpo (nunca em URL), e confere o ID token: emissor, audiência, validade, `nonce`, email verificado e, se configurado, o domínio. O token vem direto do endpoint, por isso a assinatura não é reverificada (OpenID Connect Core 3.1.3.7).
- Só entra quem **foi cadastrado** e tem papel ativo em ao menos uma empresa. A conta Google (`sub`) é **ligada no primeiro login** e precisa coincidir depois, para um email reciclado não herdar o acesso. Qualquer recusa mostra à pessoa a mesma mensagem; o motivo real vai só para a trilha.
- Erros voltam como códigos fixos na URL (`/?login_error=…`); nada do provedor é refletido. O redirecionamento depois do login é sempre para `/`.

## Sessão e requisições

- Cookie `__Host-assistente_session` em https (`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, sem `Domain`); sem o prefixo apenas em `http://localhost`, para desenvolvimento. No banco fica **só o hash** do token. Inatividade de 8 h (renovada a cada uso, gravando no máximo uma vez por minuto) e limite absoluto de 7 dias.
- **Cada requisição** relê no banco a sessão, a pessoa e o papel: sair, revogar, desativar alguém ou mudar o papel vale **na próxima requisição**.
- Toda rota `/api/team` exige: `Host` igual ao endereço público; sessão válida; `Origin` igual ao público (um GET do mesmo site sem `Origin` só passa com `Sec-Fetch-Site: same-origin`); nas alterações, corpo JSON e o cabeçalho `X-CSRF-Token` (derivado da sessão, entregue por `/auth/session`). Não há CORS.
- Limites em memória: 20 tentativas/min nos endpoints de login por cliente; 900 leituras e 90 alterações por minuto por sessão (429 com `Retry-After`).
- Cabeçalhos: `Cache-Control: no-store`, `nosniff`, `Referrer-Policy: no-referrer`, CSP restrita (`frame-ancestors 'none'`), HSTS em https, também nos redirecionamentos.

## Papéis (por empresa)

`leitor` < `atendente` < `aprovador` < `admin`. A empresa vem **da URL e é conferida com o papel guardado**; `tenant_id` no corpo é recusado. Uma empresa onde a pessoa não está responde 404 igual a uma empresa que não existe.

| Papel | Pode |
|---|---|
| leitor | ver demandas, eventos, caixa de atenção, departamentos, agentes e lacunas |
| atendente | abrir, atribuir, pausar, assumir, devolver e rejeitar demandas; registrar lacunas |
| aprovador | também decidir aprovações e resolver resultados desconhecidos de mensagens |
| admin | também configurar departamentos, agentes, concessões, conexões e credenciais da empresa, decidir lacunas e **gerenciar membros** |

**Nunca remotamente** (só o dono local, na porta local): criar/renomear empresas, instalar ou desativar skills, aprovar servidores MCP, desativar uma pessoa em todas as empresas, revogar sessões de outro usuário e ver a trilha de autenticação. Cada rota está em uma tabela (`POLICY`); **rota que não está na tabela responde 403** e um teste garante que todas as rotas registradas têm regra. Os serviços reforçam por baixo: um leitor não grava nem por uma rota liberada, e aprovação/controle/lacunas checam o papel do contexto. Tudo que a pessoa faz fica registrado com o **id dela** (decisões de aprovação, notas, eventos).

Um administrador não reduz o próprio acesso, e a empresa nunca fica sem administrador ativo. Uma pessoa pode ter papéis diferentes em empresas diferentes.

## Trilha de autenticação

Tabela `auth_events`: login, recusas (com o motivo), saída, convite, mudança de papel, desativação e revogação, só com ids e motivos (sem token, código, nome ou email). Leitura somente pelo dono local.

## Validação

- `tests/test_team_identity.py` (17): cadastro, ligação do `sub`, recusas, expiração por inatividade e absoluta, só o hash do token no banco, revogação, contexto sempre pelo papel guardado, contexto não forjável, cada papel contra os serviços (abrir demanda, aprovar, controlar, decidir lacuna), gestão de membros só da própria empresa, administrador nunca zerado.
- `tests/test_team_remote.py` (32), com um Google falso (PKCE, segredo e `nonce` conferidos) e o servidor remoto real em uma porta local: fluxo completo; segredo só no corpo da troca; login CSRF; `state` desconhecido, expirado e reutilizado; ID token com audiência, emissor, `nonce`, validade ou email inválidos; domínio do Workspace; provedor fora do ar; limites; `Host`, `Origin`, CSRF e JSON; cabeçalhos; nada do app local existe no remoto; **cada rota tem regra e cada regra tem rota**; rota não listada e ações só-locais negadas; cada papel nas rotas certas; **duas pessoas e duas empresas** (acesso por id, por URL da outra empresa, cursor/replay de eventos, aprovação, lacuna, assumir demanda, departamento de outra empresa, corpo com `tenant_id`, papéis diferentes por empresa, lista de membros); sessão revogada, papel alterado e membro removido valendo na próxima requisição; administrador sem se trancar para fora.
- `tests/test_team_remote_setup.py` (8): script de preparação, cookies (`__Host-`, `Secure`) e chave do limitador.
- `frontend/tests/auth.test.js` (4). Regressão: suíte Python completa, `npm test`, `npm run build`, testes do desktop.
- Verificado também subindo o backend real com `remote.enabled`: o listener local responde como antes, o remoto responde 401 sem sessão, 404 em `/ws`, 403 em outro `Host` e redireciona o login com `nao_configurado` quando falta o segredo.

## Limites (honestos)

- **Não foi testado com o Google de verdade nem com Tailscale**: toda a validação do fluxo usa um provedor falso. O teste real (cadastrar o email, entrar com a conta, conferir os papéis pela internet privada) é um passo manual **pendente**.
- **Escopo por departamento e identidade de worker não existem ainda** (#84): o papel vale para a empresa inteira, e a fila e os workers continuam internos ao backend, sem identidade própria de serviço (nenhuma sessão os alcança).
- Sem verificação em duas etapas própria: vale a do Google. Não há provedor além do Google.
- Os `state` pendentes e os limitadores ficam em memória de **um** processo: reiniciar derruba logins em andamento (a pessoa tenta de novo); mais de um processo exigiria armazenamento compartilhado.
- Linhas de sessões expiradas e de `auth_events` ainda não são removidas automaticamente, e não há tela para a pessoa pedir exclusão dos próprios dados (o email é dado pessoal); só é possível desativar.
- Atrás de um proxy local, todas as requisições chegam do loopback; `trust_forwarded_for` faz o limite de login usar o último IP de `X-Forwarded-For` (o acrescentado pelo proxy), apenas quando o par imediato é loopback. Só ative com um proxy de confiança que sobrescreva esse cabeçalho.
- `scripts/remote_setup.py` roda a partir do repositório; o pacote portátil não leva a pasta `scripts/`.
- O chat, a voz e o avatar do T31 não estão disponíveis remotamente.
