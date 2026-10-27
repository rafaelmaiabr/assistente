# Servidor MCP IMAP somente leitura (T18)

> **Camada de conectores:** este servidor/transporte é o driver `imap` da capacidade `email-leitura` (sem mudança de comportamento); ver [TEAM-CONNECTORS-EMAIL.md](TEAM-CONNECTORS-EMAIL.md).

Entrega da issue [#33](https://github.com/rafaelmaiabr/assistente/issues/33): `team/imap_client.py` (cliente e leitor MIME), `team/imap_mcp.py` (servidor MCP local, registro de contas, teste de conexão), `scripts/smoke_imap.py`. Usa conexões/cofre/catálogo ([TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md)), skills ([TEAM-SKILLS.md](TEAM-SKILLS.md)) e o gateway ([TEAM-GATEWAY.md](TEAM-GATEWAY.md)). Biblioteca padrão apenas (`imaplib`, `ssl`, `email`).

Estado: o servidor está implementado e testado com um servidor IMAP falso, **mas não está ligado ao `TeamApp` nem a nenhuma tela**: ainda não existe ligação do gateway real no app (ver [TEAM-GATEWAY.md](TEAM-GATEWAY.md)). Não foi validado com conta real; o roteiro está abaixo. Isto **não** é envio de email nem resposta automática (T19/T20/T21 tratam de conectores completos e OAuth).

## Ferramentas (todas `read`)

| Ferramenta | Argumentos | Resultado |
|---|---|---|
| `listar-pastas` | — | pastas autorizadas e se existem no servidor |
| `listar-mensagens` | `pasta`, `limite` (1–50), `antes_de_uid`, `desde` | UIDVALIDITY, mensagens (uid, de, para, assunto, data, tamanho) e `next_before_uid` para paginar |
| `buscar-mensagens` | `pasta`, `texto`, `remetente`, `assunto`, `desde`, `ate`, `limite`, `antes_de_uid` | idem, filtrado |
| `ler-mensagem` | `pasta`, `uid`, `uidvalidity` | cabeçalhos, texto limitado (20 000 caracteres), lista de anexos (nome, tipo, tamanho) |

Mensagem = `(pasta, UIDVALIDITY, UID)`. Se a UIDVALIDITY informada diferir da atual, o erro é `uidvalidity_mudou` (liste de novo); UID inexistente é `uid_nao_encontrado`. A leitura baixa no máximo 256 KiB por mensagem (`BODY.PEEK[]<0.N>`) e marca `truncated`.

## Somente leitura, por construção

O cliente só consegue enviar `LOGIN`, `LIST`, `EXAMINE` (abre a pasta em modo somente leitura), `UID SEARCH` e `UID FETCH` com `BODY.PEEK`, tamanho e UID. Não existe código para `STORE`, `COPY`, `MOVE`, `DELETE`, `EXPUNGE`, `APPEND`, `SELECT` ou SMTP, e o envelope de `FETCH` é validado por uma lista de itens permitidos (`RFC822` ou `BODY[]` sem `PEEK` marcariam a mensagem como lida e são recusados). O servidor falso dos testes recusa tudo isso e registra os comandos: os testes confirmam que só os permitidos foram enviados e que nenhuma mensagem foi marcada como vista.

## Conta, pastas e segredo

- O **operador** associa a uma conexão `imap` aprovada o servidor, a porta, o usuário e a **lista de pastas autorizadas** (`ImapAccounts.configure`, gravada em `imap-accounts.json` na pasta de dados da equipe, só pelo backend, com a empresa dona da conexão). O modelo não passa servidor, usuário, pasta fora da lista nem conexão: argumentos desconhecidos são recusados pelo gateway e pasta fora da lista devolve `pasta_nao_autorizada` sem tocar no servidor.
- A **senha** fica no cofre (`set_credential`) e chega ao transporte pelo gateway; não aparece em respostas, erros ou logs, e trechos de mensagens com cara de segredo são redigidos. Use **senha de aplicativo** onde o provedor aceitar; OAuth fica para T19/T20.
- **TLS obrigatório**: certificado e nome do servidor sempre verificados (`ca_file` opcional para CA privada). Sem TLS só para `127.0.0.1`/`localhost` (testes).
- **Teste de conexão** (`ImapAccounts.test`): conecta com a senha do cofre e lista as pastas. Erros acionáveis e sem credencial: `autenticacao_recusada` (use senha de aplicativo), `tls_invalido`, `tempo_esgotado`, `sem_conexao`, `pasta_nao_autorizada`, `conta_nao_configurada`.

## Conteúdo é dado não confiável

O resultado traz `untrusted: true` e chega ao modelo só como item externo de ferramenta (escapado, fora do prompt de sistema). Leitura MIME segura: HTML vira texto (scripts, estilos, comentários e tags são descartados; nada é carregado, imagens remotas não são buscadas); anexos são **listados e nunca decodificados para o resultado nem executados**; profundidade (6) e número de partes (50) limitados; cabeçalhos higienizados (sem quebras de linha); charsets inválidos tolerados.

## Catálogo

`approve_catalog` registra a entrada `imap-somente-leitura` com o novo transporte **`local`** (servidor embutido no backend: sem URL e sem launcher; adicionado ao contrato `McpTransport`). `ImapTransport` é o executor do gateway para esse transporte; o cliente bloqueante roda em thread.

## Validação

`tests/test_team_imap.py` e `tests/test_team_imap_mcp.py` com um servidor IMAP falso (`tests/imap_fake.py`): leitura e paginação, busca, Unicode, mensagem grande, UID inválido/inexistente, UIDVALIDITY alterada, pasta não concedida, comandos proibidos, timeout, login recusado, servidor sem TLS, **certificado não confiável e nome divergente** (certificado autoassinado gerado com `openssl`, teste ignorado se ausente), MIME aninhado e com partes demais, HTML hostil, anexos, cabeçalhos/charsets estranhos, chamadas pelo gateway, isolamento entre empresas.

### Roteiro com conta real (manual)

1. Crie uma **senha de aplicativo** no provedor (Gmail, Outlook, Zoho etc. exigem IMAP habilitado) e anote host/porta (normalmente `imap.<provedor>`, 993).
2. `python scripts/smoke_imap.py --host <host> --user <seu email> --folder INBOX` (a senha é pedida sem eco).
3. Esperado: "TLS e login: ok", pastas, UIDVALIDITY e remetente/assunto/data da mensagem mais recente; **a mensagem continua não lida** na caixa.
4. Teste também uma pasta que não exista no servidor (`present: false`) e uma senha errada (mensagem acionável, sem senha).

## Limites

Um servidor por conexão; sem IDLE, sem paginação por data além de `desde`/`ate`, sem busca em anexos, sem leitura de PDF/HTML remoto. Buscas usam o `SEARCH` do servidor, cuja semântica de acentos varia. Não há reconexão persistente (cada chamada abre e fecha a sessão). Não foi testado contra provedores reais nesta entrega.
