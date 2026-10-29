# O chat lê o email pelas conexões da empresa (#117): decisões

Issue [#117](https://github.com/rafaelmaiabr/assistente/issues/117), acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100). **Este documento veio antes do código** (critério de aceite 1) e as três decisões foram aprovadas. A seção **Estado da entrega** diz o que já está implementado; o resto do texto é o desenho aprovado.

## Estado da entrega

**Parte 1 (esta entrega)**, em `team/chat_mail.py`, `web_server.py` e `browser_agent.py`:

- O chat lê uma caixa de empresa pelo gateway: um agente de sistema por conexão (`Chat local <id da conexão>`, no departamento `Chat local (sistema)`), com uma habilidade somente leitura **gerada do contrato da capacidade** `email-leitura` para o driver da conexão e concedida só àquela conexão, e uma execução curta por leitura, que termina como `concluida`. Auditoria, isolamento por empresa e resultado como dado não confiável vêm do gateway.
- **Fonte:** o email pessoal, quando configurado, **sempre vence** (nada que funciona muda). Sem ele, uma única caixa de empresa utilizável (configurada, credencial ativa, ativa e **testada com sucesso**) é a que o chat lê. Com várias caixas de empresa e sem email pessoal, **nenhuma é escolhida**: as ferramentas de leitura dizem isso e nada é lido. **A escolha pela tela (cartão) é a parte 2 e ainda não existe.**
- Somente leitura: com uma caixa de empresa, `preparar_resposta` responde que não prepara nada e o prompt diz que a caixa é da empresa e só de leitura.
- Depois de conectar pelo cartão, o chat em andamento recebe a nova fonte (as ferramentas e o prompt são refeitos) e o pedido original é retomado, **somente** quando o chat de fato lê essa conexão; caso contrário o cartão e o resultado dizem por quê.
- A habilidade do chat não nomeia IMAP: um segundo driver de `email-leitura` é lido pelo mesmo código (testado com um driver falso).

**Fora desta entrega:** cartão de escolha entre várias fontes; unificar o email pessoal com as conexões; envio por conexão da empresa; qualquer escrita. Não foi feito teste com um servidor IMAP real nem com o aplicativo desktop; os testes usam um IMAP falso.

## O desenho aprovado

O estado do código antes desta entrega:

### Fatos do código antes da entrega

- **Email pessoal:** `PersonalMail` guarda servidor, usuário e pastas em `mail-account.json` e a senha no cofre sob `pessoal/email-imap`. Sem tenant, sem gateway, sem auditoria da camada. Também envia, pelo `PersonalOutbox`, com aprovação na tela.
- **Conexão da empresa:** `ConnectionService` + `ImapAccounts`, uma conta por `(empresa, conexão)`, senha no cofre por conexão. A leitura passa pelo `ImapTransport` da capacidade `email-leitura`.
- **Gateway:** só executa para uma **execução de agente** (`open_execution` exige contexto de agente e uma `agent_run` ativa) e só expõe ferramentas que o agente tem por **concessão de habilidade** a **uma conexão**. O chat não é um agente nem tem execução.
- **Cartão (#107):** `CHAT_CAPABILITIES` está vazio de propósito; a retomada do pedido só acontece para capacidades que o chat realmente usa.

## Decisão 1: como o chat escolhe a fonte

**Recomendação: a fonte é decidida pelo servidor e pela pessoa, nunca pelo modelo.**

1. As ferramentas do chat continuam as mesmas (`listar_emails`, `buscar_emails`, `ler_email`) e **não ganham argumento de fonte, empresa ou conexão**. O modelo não vê nem escolhe de onde lê.
2. Uma **fonte ativa** por conversa, mantida pelo servidor: `pessoal` ou `(empresa, conexão)`.
3. **Padrão:** se a pessoa tem só uma fonte utilizável (o email pessoal, ou uma única conexão `email-leitura` da empresa, configurada, com credencial ativa, ativa e **testada com sucesso**, o mesmo critério `existente` do cartão), essa é a fonte.
4. **Mais de uma fonte utilizável:** as ferramentas de leitura respondem "escolha a caixa" e o servidor mostra um **cartão de escolha** (como o das integrações). Só o clique da pessoa define a fonte ativa; texto do modelo, de email ou de página não define. A escolha vale para a conversa e é mostrada na tela ("lendo: <rótulo>").
5. **A empresa nunca é escolhida pelo modelo:** vem do cartão, da lista das empresas do dono local (como na #107).
6. O prompt diz de qual fonte lê, gerado da fonte ativa (como a #121), e o rótulo aparece ao apresentar emails.

## Decisão 2: leitura pela capacidade, via gateway

A issue pede "sem o chat falar direto com o IMAP" e pelo gateway. O gateway exige agente, execução e concessão. **Recomendação: um agente de sistema "chat" por empresa**, criado e concedido só pelo backend:

- Ao usar uma conexão da empresa no chat, o servidor garante (idempotente) um agente `chat-local` na empresa, com uma habilidade empacotada só de `email-leitura` (somente `read`) concedida **àquela** conexão, e abre uma `agent_run` curta por chamada (orçamento mínimo, prazo curto).
- As três ferramentas do chat viram chamadas `listar-mensagens`, `buscar-mensagens` e `ler-mensagem` pelo gateway. Aprovação (nenhuma, é `read`), auditoria, isolamento por empresa e "resultado como dado não confiável" vêm do gateway, sem código novo de segurança.
- O resultado passa por um **adaptador** que o devolve no mesmo formato que o chat já entende (uid, uidvalidity, remetente, assunto, texto), para que prompt, `preparar_resposta` e a tela não mudem.
- Trocar o driver da capacidade `email-leitura` não muda a habilidade nem as ferramentas do chat: a conexão aponta o driver (`resolve(connection, capability)`). Isso cumpre o critério pendente da #105.

**Custo e risco, ditos com franqueza:** é a maior parte da issue. Cria um agente e uma habilidade por empresa sem tela, aumenta o histórico de execuções, e depende de a persistência de atendimento estar ligada (sem ela não há empresa nem gateway: o chat segue só com o email pessoal, sem aviso falso). Alternativa mais barata, **que não cumpre o critério**: chamar o `ImapTransport` direto do chat, sem gateway, sem auditoria da camada. Não recomendo.

## Decisão 3: relação entre o email pessoal e as conexões

**Recomendação: coexistir, sem migrar nem unificar agora.**

- O email pessoal continua exatamente como é (formulário, cofre `pessoal/email-imap`, leitura e **envio** com aprovação). Nada é migrado, apagado ou reescrito; é o critério de regressão.
- O **envio** continua só pelo email pessoal. Conexões da empresa são **somente leitura** no chat; `preparar_resposta` com uma fonte da empresa responde que responder é pelo email pessoal ou pela equipe, sem inventar envio.
- Unificar (tornar o pessoal uma conexão de uma "empresa pessoal") fica fora: exigiria migrar segredo do cofre, mudar o envio e o formulário, e arriscar o que funciona. Se for desejado, é outra issue.
- Quando as duas existem, vale a Decisão 1 (cartão de escolha). Quando só a da empresa existe, o aviso "o email pessoal não está configurado" some e a fonte é a conexão.

## Fora do escopo

Escrever, mover, apagar ou marcar emails; enviar por conexão da empresa; OAuth novo; trocar modelo; resolução por capacidade nas habilidades da equipe além do que a leitura do chat precisa.

## Como o aceite será provado

Teste ponta a ponta com IMAP falso depois de conectar pelo cartão; troca de driver da capacidade sem mudar a habilidade (driver falso); duas empresas, uma nunca lê a caixa da outra; email pessoal sem regressão; somente leitura e resultado como dado não confiável pelo gateway; `CHAT_CAPABILITIES` passa a conter `email-leitura` e a ressalva do cartão sai.
