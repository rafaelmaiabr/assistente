# Consulta GET segura a rotas liberadas (issue #120)

Parte da issue [#120](https://github.com/rafaelmaiabr/assistente/issues/120), acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100). Resolve o pedido real "consegue acessar https://brasilapi.com.br/registrobr/v1/{domain} e ler o resultado?", que o assistente não tinha como atender.

## Decisões

1. **Conector HTTP, não servidor MCP.** MCP é o protocolo preferido ([TEAM-CONNECTORS.md](TEAM-CONNECTORS.md)), mas aqui não serve: não há servidor MCP para "ler uma rota pública qualquer" e escrever um só para repassar um GET não adiciona proteção, apenas outro processo. A proteção que importa (SSRF, tamanho, tipo, redirecionamentos) fica no cliente HTTP, que é pequeno e testável. Quando existir um servidor MCP confiável para um serviço específico, ele entra como driver à parte.
2. **O modelo escolhe uma rota pelo nome e informa um valor; nunca envia URL.** Cada rota é um modelo de URL aprovado pelo operador, com **um** parâmetro (`{valor}`) de um tipo conhecido (domínio, CEP, CNPJ, número). O valor é validado e codificado antes de entrar na URL.
3. **O chat usa o cliente seguro diretamente.** O chat **não passa pelo gateway** (ele só tem as ferramentas de email, a de integração e os MCPs do `config.json`), então a ferramenta `consultar_rota` chama o mesmo cliente. O driver e a capacidade na camada de conectores, para os agentes da equipe, ficam para a parte (b) desta issue.
4. **Ativação pelo operador.** As rotas vêm de um catálogo de código revisado (hoje: registro de domínio da BrasilAPI), **desligadas**, e o operador liga ou desliga, ou cadastra uma rota própria, pelo script `scripts/http_routes.py`. O chat e o modelo não alteram rotas. O painel é a parte (b).
5. **Só GET público, sem credenciais.** Nenhum cabeçalho de autenticação, cookie ou segredo é enviado. Rota que exija chave fica fora desta versão.

## O que o cliente garante (`team/connectors/http_get.py`)

- Só `https`, porta 443, sem usuário e senha na URL, host por **nome** (endereço IP literal é recusado).
- O nome é resolvido uma vez e **todos** os endereços devolvidos precisam ser públicos (`is_global`); loopback, privados, link-local (inclui metadados de nuvem `169.254.169.254`), CGNAT, multicast e reservados são bloqueados, inclusive IPv6 e IPv4 embutido em IPv6. A conexão vai **ao endereço verificado**, não a uma segunda resolução, e o certificado é verificado contra o nome original (SNI).
- Redirecionamento: no máximo 2, **só dentro da mesma origem**, e cada salto passa pelas mesmas verificações.
- Tempo total limitado, tamanho máximo da resposta (padrão 64 KB), sem compressão, só `application/json` e `text/plain`.
- Sem proxy, sem cabeçalhos além de `Host`, `Accept`, `User-Agent` e `Accept-Encoding: identity`.
- Erros tipados e sem detalhe interno; a resposta chega ao modelo marcada como **dado não confiável**.

## Como usar

```
python scripts/http_routes.py list
python scripts/http_routes.py enable registro-dominio
```

Reinicie o aplicativo. Depois, no chat: "consulte o registro do domínio exemplo.com.br". O assistente só recebe a ferramenta `consultar_rota` (e a lista de rotas no prompt) **quando há ao menos uma rota ligada**; sem rota ligada ele não oferece nem afirma nada disso. Rotas próprias: `scripts/http_routes.py add <nome> "<descrição>" "https://…/{valor}" <dominio|cnpj|cep|numero>`, que ficam desligadas até o `enable`.

## Validação real (feita)

Uma consulta pelo cliente seguro a `https://brasilapi.com.br/api/registrobr/v1/h1internet.com.br` (o domínio citado na conversa de teste): HTTP 200, JSON, campos `fqdn`, `status` (`REGISTERED`), `hosts`, `expires-at`, `suggestions` e outros. Também mostrou um erro que os testes locais não pegariam: **a URL `https://brasilapi.com.br/registrobr/v1/{domain}`, sem `/api/`, responde 404 em HTML** (o cliente a recusa como tipo não aceito). A rota revisada usa `/api/registrobr/v1/{valor}`.

Limite desta validação: a página de documentação da BrasilAPI só carrega com JavaScript e não pôde ser lida; o caminho foi confirmado pela **resposta real** da API, não pela documentação. Foi uma consulta pontual, não um teste de carga nem de estabilidade do serviço.

## Estado real e limites

- Os testes automáticos usam servidor HTTPS local falso (certificado gerado no teste) e resolvedor simulado: nenhum serviço real é chamado por eles. A consulta real acima foi manual, uma vez.
- Parte (b), **fora** deste PR: capacidade `consulta-http` e driver no registro de conectores (gateway, auditoria, isolamento por empresa) e cadastro de rotas no painel. Por isso este PR **não fecha** a #120.
- Uma rota tem um parâmetro; consultas com vários parâmetros, cabeçalhos ou autenticação ficam fora.
- O DNS pode mudar entre a verificação e a conexão: a conexão usa o endereço já verificado, então isso não abre brecha, mas um serviço com DNS instável pode falhar e o erro é "dns".
