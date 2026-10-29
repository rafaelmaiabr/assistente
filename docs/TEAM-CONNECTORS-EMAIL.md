# Email na camada de conectores (C5)

Parte da issue [#105](https://github.com/rafaelmaiabr/assistente/issues/105), do acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100): `team/connectors/mail.py`. Reenquadra o que já existe ([TEAM-IMAP.md](TEAM-IMAP.md), [TEAM-SMTP.md](TEAM-SMTP.md), [TEAM-OAUTH.md](TEAM-OAUTH.md)) na camada ([TEAM-CONNECTORS.md](TEAM-CONNECTORS.md)). **Nenhum comportamento mudou**: o servidor IMAP, o SMTP, o OAuth, o chat de email e a segurança são os mesmos.

> **Estado real:** os drivers estão descritos e registráveis, mas o registro **não está ligado ao `TeamApp`** (como o IMAP e o gateway ainda não estão). Validado só com servidor IMAP falso; nenhuma conta real. Trocar de fornecedor de email **ainda exige atualizar a skill** (ver limites).

## Duas capacidades, um driver cada

| Capacidade | Driver | Protocolo | O que faz |
|---|---|---|---|
| `email-leitura` | `imap` | MCP (transporte `local`, em processo) | as quatro ferramentas `read` de sempre: `listar-pastas`, `listar-mensagens`, `buscar-mensagens`, `ler-mensagem` |
| `email-envio` | `smtp` | SMTP (não MCP) | `enviar-mensagem` (`send`) |

- **Leitura.** O contrato é **derivado de `imap_mcp.TOOL_SPECS`**, então nomes, argumentos e classificação que o gateway vê são exatamente os aprovados antes da camada (teste confirma a igualdade, a chave de catálogo `imap-somente-leitura` e a versão `1.0.0`). O driver usa o `ImapTransport` já existente. O IMAP segue somente leitura por construção, com pastas autorizadas, TLS obrigatório e conteúdo tratado como dado não confiável.
- **Envio.** O SMTP **não é MCP e não é executado pelo gateway**: a mensagem sai pelo serviço de saída (rascunho, aprovação exata de uso único, revalidação imediata, reconciliação; `team/outbound.py`, `team/smtp_transport.py`). O descritor registra o motivo (`non_mcp_justification`) e **não entra no catálogo MCP** (`approve` recusa). Ele existe para que a conexão SMTP de uma empresa resolva para um driver e para que um segundo driver de envio (por exemplo, um servidor MCP de email que envie) tenha de cumprir o mesmo contrato: ferramenta `enviar-mensagem` classificada `send`, `para` ligado ao destinatário da aprovação, todos os argumentos obrigatórios e `chave` como chave de idempotência.

## Chave de catálogo própria do driver

A camada passou a aceitar `catalog_key` no descritor (padrão: o provider). O IMAP o usa para manter `imap-somente-leitura`, a chave que os catálogos e as skills já referenciam, enquanto o provider da conexão continua `imap`. O roteador `local` despacha por essa chave. Um segundo driver não pode repetir um provider nem uma chave de catálogo já usados.

## Como acrescentar outro fornecedor de email

Crie um driver para `email-leitura` e/ou `email-envio` (MCP primeiro; fora do MCP, com justificativa), registre-o em `register_email_drivers` ou ao lado dele e rode o kit de contrato ([TEAM-CONNECTORS.md](TEAM-CONNECTORS.md)). O contrato rejeita: ferramenta de leitura ausente ou reclassificada, argumento com tipo ou obrigatoriedade diferentes, envio sem destinatário na aprovação, sem chave de idempotência ou com argumento exato opcional.

## Limites

- **Skills podem nomear a capacidade (#105, resolução por capacidade).** Além da forma antiga (`imap-somente-leitura.listar-mensagens`, que continua valendo), uma skill pode declarar `email-leitura.listar-mensagens`. `SkillService.serves(entry, server)` aceita a chave de catálogo **ou** a capacidade que o driver da conexão serve (`ConnectorRegistry.capability_of`); a concessão e o gateway usam a mesma regra, então a **conexão**, nunca o modelo, decide qual driver responde. Uma skill, dois drivers: testado com o driver IMAP e um segundo driver falso (`tests/test_team_capability_skills.py`). Salvaguardas: o nome de uma capacidade não pode ser a chave de catálogo de um driver de **outra** capacidade (nem o contrário); chave igual à própria capacidade é permitida; servidor MCP aprovado à mão não tem capacidade; nome de outra capacidade ou ferramenta que o driver não oferece é recusado na concessão; sem o resolvedor (`SkillService` isolado) só vale a chave de catálogo. Aprovação, auditoria (o alias aparece como a skill o escreveu) e isolamento por empresa não mudaram. **Fora desta entrega:** a habilidade gerada para o chat (`team/chat_mail.py`) ainda é por driver; migrá-la para a forma por capacidade é uma troca pequena, mas fica para depois para não conflitar com a #117.
- O descritor tem um único tipo de autenticação (`secret`); senha de aplicativo e OAuth (XOAUTH2) continuam escolhidos por conta pelo operador, como antes.
- O driver SMTP não tem transporte nesta camada, então o kit de contrato não o exercita: ele valida o descritor e o contrato; o envio real segue coberto pelos testes do serviço de saída e do SMTP (servidor falso).
- Sem leitura nem envio com conta real; sem ligação ao `TeamApp`; Graphify não atualizado.
