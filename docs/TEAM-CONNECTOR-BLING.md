# Driver Bling (#104): decisões antes do código

Issue [#104](https://github.com/rafaelmaiabr/assistente/issues/104), acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100). **Este documento não implementa nada.** Cumpre o primeiro critério de aceite ("decisões registradas antes do código; nenhuma chamada a endpoint não documentado"). O código só começa depois da aprovação das decisões e das respostas do operador na seção final.

Fontes consultadas em 2026-10-29 (todas oficiais do Bling). Onde a página não trazia o dado, está dito **não confirmado**: nada abaixo foi testado contra uma conta real.

- [Aplicativos e OAuth](https://developer.bling.com.br/aplicativos), [Migração para JWT](https://developer.bling.com.br/migracao-jwt)
- [Limites](https://developer.bling.com.br/limites), [Webhooks](https://developer.bling.com.br/webhooks)
- [Servidor MCP](https://developer.bling.com.br/mcp-server), [Homologação](https://developer.bling.com.br/homologacao)
- [Anúncio do Bling MCP](https://blog.bling.com.br/?p=21609)

## Decisão 1: MCP oficial ou servidor nosso?

**O que a documentação diz.** Existe um servidor MCP **oficial**: `https://mcp.bling.com.br/mcp`, transporte só Streamable HTTP, autenticação **OAuth 2.1 com PKCE** feita pelo cliente MCP com a conta Bling da pessoa. Qualquer cliente MCP pode conectar. Usa os limites da API do plano, sem custo extra. Ferramentas de leitura citadas: `listSalesOrders`, `getSalesOrder`, `listNfes`, `listProducts`, `listAccountPayables`, `listAccountReceivables`, `listStatusModules`, `listStatusModuleTransitions`. As de escrita não são nomeadas (a página fala de criar/alterar contatos, pedidos de venda, pedidos de compra e entradas de estoque). Segundo o anúncio, a escrita pede confirmação do usuário no cliente.

**Por que isso não decide sozinho.** O contrato `erp` ([TEAM-CONNECTORS-ERP.md](TEAM-CONNECTORS-ERP.md)) exige `consultar-clientes`, `consultar-cobrancas`, `status-cobranca`, e exige que a escrita ligue cliente, valor e moeda à **aprovação exata** do nosso gateway, com chave de idempotência e reconciliação. Isso é nosso, não do servidor do Bling. E o servidor oficial:

- usa OAuth 2.1/PKCE **interativo do cliente MCP**, enquanto nossa camada guarda o token de renovação no cofre e renova sozinha (#34/#35). Não confirmado se um cliente nosso consegue registrar-se no fluxo dele (registro dinâmico de cliente) sem um aplicativo aprovado;
- não lista `consultar-clientes` nem `status-cobranca` por esses nomes, e a lista completa de ferramentas **não está na documentação** (a página manda perguntar ao assistente);
- tem confirmação de escrita no cliente, que **não substitui** nossa aprovação com hash do payload, uso único e dry-run.

**Recomendação.** Duas etapas, na ordem da própria issue (a) leitura, (b) escrita:

1. **Leitura (a):** um **adaptador MCP nosso por cima do servidor oficial**, se (e somente se) a pergunta do OAuth acima for respondida "sim" na validação com a conta do operador. O adaptador mapeia `listAccountReceivables`, `listSalesOrders` etc. para as ferramentas do contrato `erp`, sem expor as ferramentas do Bling ao modelo. Justificativa: não duplicar a API, herdar as permissões já definidas no Bling.
2. **Se o OAuth do servidor oficial não servir a um cliente nosso:** **servidor MCP local nosso** encapsulando a API REST v3 (`https://api.bling.com.br/Api/v3/...`), como o driver `http` da #120 (MCP local em processo). É o caminho já usado no projeto e o que nos dá controle do OAuth no cofre.
3. **Escrita (b):** sempre pelo nosso gateway e pelas nossas aprovações; **nunca** delegar a confirmação à do servidor do Bling. Dry-run por padrão. Só depois de a leitura estar validada, em PR próprio.

## Decisão 2: API, autenticação, escopos, limites e paginação

| Tema | O que a documentação oficial diz | Estado |
|---|---|---|
| Versão e base | API v3, `https://api.bling.com.br/Api/v3/...`, `Authorization: Bearer <token>` | confirmado |
| Fluxo OAuth | Somente **Authorization Code**. Código vale **30 segundos**. Resposta com `access_token` e `refresh_token` | confirmado |
| Credenciais do app | `client_id:client_secret` em Base64 no cabeçalho **Basic**; é proibido enviá-las no corpo | confirmado |
| Refresh token | Expira em **30 dias**. Renovação: POST no mesmo endpoint `/token` com o refresh token no corpo | confirmado; **nomes exatos dos parâmetros não confirmados** |
| Access token | Há expiração, **valor não informado** na documentação | não confirmado |
| URLs de autorização e de token | Citados como `authorize` e `/oauth/token`; **host e caminho completos não aparecem** nas páginas lidas (uma fonte de terceiros cita `www.bling.com.br/Api/v3/oauth/...`) | **não confirmado: não codificar sem ver o exemplo oficial** |
| JWT | Token opaco está sendo descontinuado. Usar o cabeçalho `enable-jwt: 1` na emissão, na renovação e em toda chamada. JWT tem de 1.500 a 3.000 caracteres | confirmado |
| Escopos | Escolhidos no cadastro do app; só se acessa o que o usuário autorizou. **Salvar o app com escopos alterados revoga todos os usuários**, que precisam autorizar de novo. Nomes dos escopos de leitura de clientes/pedidos/contas a receber **não confirmados** | parcial |
| Revogação | `POST /oauth/revoke`, Basic. `revoke_action` e `revoke_target` revogam tudo de um usuário ou empresa | confirmado; detalhes não |
| Limites | **3 requisições por segundo** e **120.000 por dia**, por conta, somados em todos os módulos. Estourou: **429** e as seguintes não são processadas | confirmado |
| Bloqueio de IP | 300 erros em 10 s ou 600 requisições em 10 s: 10 min; 20 chamadas a `/oauth/token` em 60 s: **60 min**; reincidência pode bloquear sem prazo | confirmado |
| Filtros de data | GET com intervalo maior que **um ano** devolve 400 | confirmado |
| Paginação | Parâmetro `pagina`, padrão de até **100** registros por chamada; `limite` **não confirmado** | parcial |

**Consequências para o desenho (a confirmar com a conta real):**

- Limitador próprio de **no máximo 3 req/s por conta Bling** (e contagem diária), com recuo exponencial no 429, antes de qualquer outra coisa. Renovar o token **uma vez por vez e com trava**, para não chegar a 20 chamadas/min em `/oauth/token` e bloquear o IP por uma hora.
- Perfil OAuth **novo** em `team/oauth.py` para o Bling, no mesmo serviço da #34/#35, com o **token de renovação só no cofre**, e aviso de que o refresh token expira em 30 dias sem uso (reautorização necessária).
- O aplicativo Bling **não é criado automaticamente**: o operador cria na Área do Integrador e informa `client_id`/`client_secret` pelo cofre.
- Escopos **mínimos** por operação: leitura primeiro; os de escrita só na parte (b). Mudar escopos revoga usuários, então a parte (b) exige reautorização.
- Erros do Bling (400, 401, 429, 5xx) mapeados para erros tipados sem repassar corpo nem dados do cliente.

## Decisão 3: ambiente de teste e custo

- **Não há sandbox de dados.** A documentação não menciona conta de teste. Existe uma **API de homologação** (`/Api/v3/homologacao/produtos`), que serve para o **processo de aprovação do aplicativo**: uma sequência de requisições (GET, POST, PUT, PATCH, DELETE) em até 10 s total e com 2 s entre chamadas, incluindo invalidar o token e renová-lo. Depois, a equipe do Bling revisa o aplicativo (logo, descrições, links, manual, vídeo de demonstração, escopos).
- **Consequência:** os testes automáticos usam **fixtures gravadas/sintéticas** e um provedor OAuth falso. Teste real só com a conta que o operador autorizar, e o PR dirá "feito" ou "**não feito**". A API de homologação **não** valida leitura de pedidos ou contas a receber.
- **Custo:** a documentação não cita preço para API nem para o MCP (o anúncio diz que o MCP está incluído em todos os planos). Os módulos disponíveis dependem do plano da conta. Não confirmado qual plano o operador tem.
- Se o aplicativo for só de uso próprio (não publicado), **não confirmado** se a revisão/aprovação do Bling é exigida; a página de homologação descreve o fluxo para publicação.

## Decisão 4: webhooks ou polling

- **Webhooks existem** e são configurados por servidor, recurso e ação na aba "Webhooks" do aplicativo. Recursos documentados: `order`, `product`, `stock`, `virtual_stock`, `product_supplier`, `invoice`, `consumer_invoice`. Ações: `created`, `updated`, `deleted` (status "excluído" gera `updated`).
- **Contas a receber não aparecem na lista de webhooks.** Como a baixa de cobrança é o centro do #42, **a prova de pagamento não pode depender de webhook**: a decisão é **polling autorizado de contas a receber** (`apply_snapshot`, que já existe e é idempotente), e webhook só para pedidos e notas fiscais, se o operador quiser.
- **Autenticação:** cabeçalho `X-Bling-Signature-256: sha256=<hex>`, HMAC-SHA256 do corpo JSON com o **client secret** do app. `verify_hmac_sha256(..., prefix="sha256=")` já cobre isso. Não confirmado se o HMAC é sobre os bytes brutos ou sobre o JSON reserializado; **vale o corpo bruto** até a conta real mostrar o contrário.
- **Entrega:** sucesso é `2xx` em até **5 s**; repete com intervalos crescentes por até **3 dias** e, depois, **desativa o webhook** até alguém reativar à mão. Pode duplicar e chegar fora de ordem. Isso casa com o `Reconciler` (`duplicado`, `ja_aplicado`, `obsoleto`, `aguardando_ordem`).
- Campos do evento: `eventId`, `date`, `version`, `event`, `companyId`, `data`.
- **Rota HTTP que receba eventos não existe** no projeto (ver [TEAM-CONNECTORS-ERP.md](TEAM-CONNECTORS-ERP.md)) e o app roda local. Receber webhook exige um endereço público, o que é decisão do operador. **Recomendação:** começar por polling; webhook fica como fase posterior e fora desta issue se exigir expor o aplicativo.

## Divisão proposta da #104

| PR | Conteúdo | Depende de |
|---|---|---|
| 1 (este) | Este documento | aprovação das decisões |
| 2 | Perfil OAuth do Bling + limitador de taxa + erros tipados, testados com provedor falso | URLs/parâmetros de OAuth confirmados |
| 3 | (a) Leitura: `consultar-clientes`, `consultar-cobrancas`, `status-cobranca`, `consultar-pedidos` via gateway, com fixtures | PR 2 |
| 4 | Polling de contas a receber alimentando o `Reconciler` | PR 3 |
| 5 | (b) Escrita com aprovação exata, dry-run, reconciliação, resposta perdida | PR 3 e conta autorizada |

A #104 só fecha quando todo o aceite estiver cumprido; a #42 só fecha se o aceite dela também estiver. Nada aqui usa `Closes #100`.

## Decisões que precisam do operador

1. **Conta Bling autorizada** para validação real? Qual plano (quais módulos: Financeiro, Vendas)? Sem conta, os PRs 2 a 5 declaram a validação real como **não feita**.
2. **Aplicativo Bling:** o operador cria o app na Área do Integrador e entrega `client_id`/`client_secret` pelo cofre; o aplicativo é só de uso próprio ou será publicado (homologação)?
3. **Operações liberadas:** só leitura agora? Escrita (emitir/alterar cobrança ou pedido) só depois, em PR próprio?
4. **Webhook:** aceita ficar só com polling por enquanto?
5. **MCP:** aprova a ordem "adaptador sobre o MCP oficial se o OAuth servir; senão servidor local nosso"?

## Limites deste documento

- Nenhuma chamada ao Bling foi feita; nada foi testado com conta real. O texto vem da leitura das páginas oficiais, extraídas automaticamente, e por isso os itens marcados **não confirmado** devem ser conferidos nos exemplos oficiais antes do PR 2.
- As ferramentas de escrita do servidor MCP oficial não estão documentadas por nome.
