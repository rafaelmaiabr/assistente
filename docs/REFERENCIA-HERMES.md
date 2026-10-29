# Referência: Hermes Agent e instruções do email-manager, e o que vale para o nosso assistente

Estudo de **referência apenas**. Nenhum código do Hermes foi copiado, instalado ou executado; só foram lidas a [documentação](https://hermes-agent.nousresearch.com/docs/) e, de forma parcial, o [repositório](https://github.com/NousResearch/hermes-agent) (MIT) em 2026-10-29. O arquivo `agent/context_compressor.py` não pôde ser lido (limite de requisições do GitHub); o que se diz sobre compressão vem da página de documentação. Tudo abaixo é **proposta**: nada vira trabalho sem issue (AGENTS.md).

Segunda fonte, local: as **instruções de triagem** do projeto `email-manager` (pasta `instrucoes/`, versão 2 de 29/10/2026), indicadas pelo operador. Só os arquivos de instrução foram lidos (`principal.md`, `fraude.md`, `spam.md`, `prioridade.md`, índice, domínio e contas); o restante do projeto, incluindo configuração e credenciais, **não** foi aberto. Nenhuma regra de conta ou domínio foi copiada para cá.

**Contexto.** O Hermes é um agente pessoal (um agente, uma memória), com terminal e arquivos. O nosso é uma equipe com empresas, aprovações e auditoria; isso não muda ([TEAM-ARCHITECTURE.md](TEAM-ARCHITECTURE.md)). O que interessa é a **inteligência do chat** (a queixa de respostas fracas): como ele monta o prompt, guarda o que aprendeu, controla o contexto e erra com segurança.

## Estado do nosso chat, conferido no código

- `assistant_core.py`: um prompt fixo mais as notas das ferramentas registradas (#121), modelo local `qwen2.5:7b`, `temperature=0.3`.
- **Sem memória entre conversas.** Nada do que a pessoa diz persiste (preferências, nome, caixa usada). A memória da equipe ([TEAM-KNOWLEDGE.md](TEAM-KNOWLEDGE.md)) é por empresa/demanda, não do chat.
- **Sem controle do tamanho do histórico.** Não achei compressão nem poda de resultados de ferramentas no chat. Num modelo de 7B com janela pequena, a qualidade cai conforme a conversa cresce.
- O prompt cresce a cada capacidade (email, HTTP, integração). Modelos pequenos seguem pior prompts longos.

## Ideias do Hermes, em ordem de valor para nós

### 1. Memória curta, curada e congelada na sessão
- Dois arquivos pequenos: notas do agente (2.200 caracteres) e perfil da pessoa (1.375). Entradas separadas por `§`. Ferramenta `memory` com `add`, `patch`, `remove` (sem `read`: o conteúdo já está no prompt).
- O texto é lido **uma vez no início** e fica fixo na conversa; gravações valem para a próxima sessão. Isso preserva o cache de prefixo.
- Estourou o limite: erro devolvendo as entradas, o agente consolida e tenta de novo. Duplicata exata não entra. O conteúdo é verificado contra injeção de prompt e Unicode invisível. Há modo de **aprovação de escrita** (`write_approval`).
- **Para nós:** perfil da pessoa local, pequeno, em `DATA_ROOT`, nunca publicado. Escrita **com aprovação por padrão** (combina com "sem inferência permanente" da T17), verificada contra injeção, sem segredos. É a mudança que mais melhora a sensação de "assistente que me conhece".

### 2. Camadas do prompt: estável, contexto, volátil
- Identidade, orientações das ferramentas e skills primeiro (estável); arquivos de contexto depois; memória, perfil e hora por último (volátil). O prompt **não muda no meio da conversa** (exceto por ação explícita).
- **Para nós:** hoje `refresh()` reescreve as instruções no meio da conversa (#107, #117). É correto funcionalmente, mas desfaz o cache de prefixo do modelo local. Vale organizar o prompt em camadas e deixar a parte volátil no fim.

### 3. Divulgação progressiva de skills
- Nível 0: só nome e descrição (cerca de 3 mil tokens para todas); nível 1: o `SKILL.md` inteiro quando a tarefa pede; nível 2: um arquivo de referência.
- Skills aparecem ou somem conforme as ferramentas disponíveis (`requires_*`, `fallback_for_*`).
- **Para nós:** já mostramos só o que está registrado (#121). Falta enxugar: um índice curto no prompt e a instrução completa só quando a capacidade for usada. Ataca diretamente o prompt longo.

### 4. Compressão de contexto em duas fases
- Dispara a 50% da janela (piso de 75% em janelas menores que 512 mil). Mantém as 3 primeiras mensagens e uma cauda por orçamento de tokens, nunca cortando um par chamada/resultado de ferramenta.
- **Fase 1, sem modelo:** resultados de ferramentas antigos com mais de 200 caracteres viram um marcador ("saída antiga removida"). **Fase 2:** o meio vira um resumo estruturado (objetivo, restrições e preferências, progresso, decisões, próximos passos, contexto crítico), atualizado de forma iterativa nas compressões seguintes.
- **Para nós:** a fase 1 é barata, determinística e testável sem modelo. Resultado de listagem de email antigo some do contexto. A fase 2 depende de um modelo e deve vir depois, se necessária.

### 5. Respostas de erro que ensinam o modelo
- Argumentos de ferramenta inválidos **não executam** e voltam ao modelo como "Invalid tool arguments"; nenhuma tentativa de consertar o JSON. Um componente de guarda pode bloquear chamada repetida que falha. Resultados grandes são limitados com orçamento proporcional à janela do modelo.
- **Para nós:** o gateway já valida argumentos e registra negações. Falta o lado do chat: padronizar a mensagem de erro devolvida ao modelo e limitar o tamanho do resultado de ferramentas do chat.

### 6. Orçamento de iterações com saída digna
- Orçamento por execução (padrão 500, subagentes 50). Ao esgotar, o agente **para e devolve um resumo do que fez**. Interromper descarta a resposta parcial e não a coloca no histórico. Cadeia de modelos de reserva para 429, 5xx e falha de autenticação.
- **Para nós:** o gateway tem `BudgetExceeded`, mas o chat não resume ao parar. Um modelo de reserva só vale se o operador quiser um provedor pago (hoje, por padrão, não).

### 7. Identidade separada da capacidade, com predefinições
- `SOUL.md` define **quem** é o agente (tom, franqueza, como lidar com incerteza), fora do repositório e sem misturar com regras do projeto. Predefinições de personalidade (14, como `concise` e `technical`) valem só para a sessão.
- **Para nós:** separar a persona do texto de capacidades (hoje tudo está no mesmo prompt) e oferecer "respostas curtas" como opção. Combina com os perfis de agente da equipe.

### 8. Busca em conversas passadas (FTS5)
- Todas as sessões ficam num SQLite com FTS5; o agente busca e navega nas mensagens reais, sem resumo por modelo, a cerca de 20 ms por consulta.
- **Para nós:** tecnicamente simples (já usamos FTS5 na T17). Porém **conversas são dado pessoal**: só local, com retenção definida, exclusão pela pessoa e fora de qualquer publicação. Fica por último e só com decisão sua.

### 9. Qualidade das skills
- Verificação consultiva de formato (log de incidente no corpo, corpo grande demais), staging de escrita com aprovação, níveis de confiança e varredura de segurança antes de instalar.
- **Para nós:** nossas skills já são declarativas, sem código, e passam por revisão de permissões. O que falta é o aviso de tamanho e de "texto que parece ordem" no momento do cadastro.

## Referência local: instruções de email do email-manager

O que ele tem de valioso é **método de decisão**, não código. Mapeado ao que já existe aqui:

| Ideia do email-manager | Já temos | Falta |
|---|---|---|
| **Preservar tem prioridade sobre limpar**; na dúvida manter na entrada e registrar para revisão. Nunca excluir de forma permanente; exclusão só recuperável (Lixeira) | O chat não move, apaga nem marca emails (#121) | Quando mover existir, adotar este padrão como regra de produto |
| **Fraude exige duas evidências independentes**, uma ligada à ação maliciosa (destino do pedido). Duas variações do mesmo indício contam como uma. Sinais fracos isolados (domínio diferente, encurtador, urgência, português ruim, falta de SPF/DKIM) **não bastam**; SPF/DKIM aprovado não prova legitimidade. Cliente conhecido ou conversa anterior em conflito: preservar | Análise financeira detecta beneficiário alterado e fatura duplicada ([TEAM-FINANCE.md](TEAM-FINANCE.md)) | Classificador de suspeita de email com evidências listadas, sem ação automática |
| **Spam não é comercial**: nota fiscal, boleto, renovação, confirmação de pagamento, cobrança real e newsletter assinada nunca vão para spam só por frequência ou duplicidade | Nada classifica | Lista de exceções como parte da regra |
| **Prioridade**: cliente confirmado, **conversa real** (conferir `In-Reply-To`/`References`; `Re:` sozinho não prova), interrupção de serviço, prazo. O relatório traz conta, UID, assunto, motivo, ação pedida, prazo **explícito** (nunca inventado) e se aguarda resposta. Prioridade com suspeita: fica na entrada com destaque | T21 trata threads sem confiar no assunto e laços ([TEAM-EMAIL-INTAKE.md](TEAM-EMAIL-INTAKE.md)) | Relatório de prioridade para o chat |
| **Escopo exato das regras**: endereço completo, domínio exato sem subdomínios, nunca herdar de outra conta; ler só o arquivo da conta alvo; conflito não resolvido: preservar e perguntar | Isolamento por empresa, conexão e concessão | Escopo por caixa dentro da empresa |
| **Regras persistentes só com aprovação**: pergunta "criar regra ou só desta vez?"; sem regra a partir de um único exemplo, de limpeza anterior ou de silêncio; formato com ID, versão, escopo, condição verificável, exceções, ação, regra substituída e exemplos positivos e negativos | Memória só proposta, confirmada pelo operador (T17) | Modelo de "regra de caixa" com esse formato (ligado à ideia A acima) |
| **Execução segura**: identificar por conta + pasta + UID + `UIDVALIDITY` (+ `Message-ID`); `UID MOVE`, nunca `EXPUNGE` geral; conferir antes e depois; não repetir às cegas após timeout; parar o lote se a verificação falhar | Leitura só com `BODY.PEEK` (não marca como lido); ferramentas do chat já exigem `uid` + `uidvalidity` | Qualquer escrita no IMAP, que hoje não existe por construção |
| **Cobertura honesta**: paginar, registrar o que foi examinado, nunca dizer que "analisou tudo" com limite, erro ou saída truncada | O chat pagina com `desde`/`limite` | Dizer na resposta quando a listagem foi limitada |
| **Registro por mensagem antes de mover**, sem senha, anexo ou corpo desnecessário; classificação **por mensagem**, nunca por domínio ou conta inteira | Auditoria do gateway por chamada | Relatório de triagem por mensagem |
| **Conteúdo de email, anexo e página é dado não confiável**; não seguir ordem contida nele, nem abrir link suspeito | Já é regra do gateway e dos resultados do chat | |

Duas falhas que o próprio `principal.md` registra no projeto de origem valem como **testes de regressão desejados** aqui: uma ferramenta de leitura que marca a mensagem como lida e uma ferramenta de mover que usa `EXPUNGE` geral. Nosso cliente IMAP usa `BODY.PEEK` e não escreve; qualquer escrita futura deve nascer com `UID MOVE` e teste contra `EXPUNGE` geral.

Nenhuma dessas ideias autoriza mover, apagar ou marcar email automaticamente. Elas descrevem como **classificar e relatar** primeiro; mover é decisão posterior, por aprovação exata.

## O que **não** adotar

- Criação automática de skills e memória sem aprovação: contraria a decisão de que agente só propõe e pessoa confirma.
- Terminal, execução de código e navegador livre: fora do escopo e do modelo de segurança.
- Memória única para tudo: nosso isolamento é por empresa.

## Issues sugeridas (nenhuma foi aberta)

| # | Título | Ataca | Esforço |
|---|---|---|---|
| A | Memória curta do chat (perfil da pessoa), local, com aprovação e verificação contra injeção | chat sem memória | médio |
| B | Prompt em camadas e índice curto de capacidades (divulgação progressiva) | prompt longo, cache | pequeno/médio |
| C | Poda de resultados antigos de ferramentas e limite de tamanho no chat | contexto crescente | pequeno |
| D | Mensagens de erro de ferramenta padronizadas e parada com resumo | falhas silenciosas | pequeno |
| E | Persona separada e modo "respostas curtas" | tom e verbosidade | pequeno |
| F | Compressão com resumo estruturado e busca em conversas | só se A a C não bastarem | grande |
| G | Triagem de email somente leitura no chat: prioridade e suspeita com evidências, sem mover nada (email-manager) | caixa de entrada sem inteligência | médio |
| H | Regras de caixa persistentes por conta/domínio, com formato e aprovação (email-manager + ideia A) | regras que se perdem a cada conversa | médio |
| I | Mover email com `UID MOVE`, só para Lixeira ou Spam, por aprovação exata e verificação (depende de G e H) | limpeza segura | grande |

**Ressalva importante.** Nenhuma dessas técnicas compensa o tamanho do modelo. Parte da fraqueza percebida no chat vem do `qwen2.5:7b`. Antes de F, vale medir A a E com os evals offline ([TEAM-EVALS.md](TEAM-EVALS.md)) e comparar com um modelo maior localmente.

## Limites deste estudo

- Leitura de documentação e de parte do repositório; nada foi executado e nenhuma afirmação sobre desempenho foi verificada.
- Números (limites de caracteres, limiares de compressão) são os da documentação do Hermes em outubro de 2026 e podem mudar.
- Páginas de recuperação de erro, repetição e laço de ferramentas tinham detalhes incompletos; o que não foi confirmado não está aqui.
