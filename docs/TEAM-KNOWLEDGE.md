# Memória e base de conhecimento (T17)

Entrega da issue [#32](https://github.com/rafaelmaiabr/assistente/issues/32): `team/knowledge.py` (`KnowledgeService`), migração `0013`. Usa contratos, acesso, demandas e o compilador de contexto ([TEAM-CONTRACTS.md](TEAM-CONTRACTS.md), [TEAM-ACCESS.md](TEAM-ACCESS.md), [TEAM-CASES.md](TEAM-CASES.md), [TEAM-AGENTS.md](TEAM-AGENTS.md)). Ainda não há tela (a gestão é pela API do serviço) e a recuperação ainda não está ligada automaticamente ao runtime: quem monta a execução chama `retrieve` e entrega `external_items` ao `compile_context`.

## Três coisas separadas

1. **Histórico da demanda**: continua nas tabelas da demanda; `retrieve(case_id=…)` devolve as últimas entradas apenas se o chamador pode ler a demanda.
2. **Memória confirmada** (`memory_item`): preferências e fatos datados por escopo `empresa`, `departamento` ou `cliente`, com fonte, data de observação e validade opcional. **Fato datado exige validade**.
3. **Conhecimento publicado** (`knowledge_document`): documentos versionados por `doc_key`, com título, fonte, tópico, departamento opcional (sem departamento = toda a empresa), validade opcional e hash do conteúdo. Publicar a próxima versão (correção) marca a anterior `substituido` e a tira do índice; a versão e a fonte aparecem em cada resultado.

## Busca local (SQLite FTS5)

`knowledge_index` é um índice FTS5 (sem acentos, sem diferenciar maiúsculas) mantido na mesma transação do documento: só documentos **publicados** estão nele. A consulta do usuário vira palavras entre aspas unidas por OR; operadores, `NEAR`, `*`, prefixos de coluna e SQL no texto são inertes. O banco é a autoridade: toda linha do índice é conferida na tabela de documentos (publicado, vigente, mesma empresa, departamento autorizado), então uma linha de índice indevida não vaza conteúdo. Vale só para o SQLite (um adaptador PostgreSQL traria sua própria busca).

## Autorizar antes de recuperar, com limites

`retrieve(ctx, consulta, …)`: o departamento de um **agente** vem da definição dele (um argumento tentando outro departamento é ignorado); o operador pode informar. Só entram documentos da empresa e do departamento autorizado; documentos de outra empresa nunca. Limite de itens (6) e de caracteres (4000) com marcador `truncated`; cada trecho tem até 600 caracteres. Documentos vencidos deixam de ser servidos antes mesmo da varredura.

## Dado não confiável

Todo item retornado traz `untrusted: true`, fonte, versão e data. `external_items` o entrega ao `compile_context` como conteúdo externo: **escapado, rotulado e fora do system prompt**; um documento não consegue fechar o bloco de dados nem mudar política, permissão ou regras. Na publicação, texto que parece instruir o assistente ("ignore as regras", "SYSTEM:", "conceda acesso", tags…) é recusado, a menos que o operador aceite o risco explicitamente: então o documento é marcado `suspicious`, aparece com aviso e continua sendo apenas dado (heurística de defesa em profundidade, não garantia). Fontes que se contradizem sobre o mesmo `topic` são marcadas `conflict` no resultado e na entrega ao modelo.

## Memória: sem inferência permanente

Qualquer agente pode **propor** uma memória (`propose_memory`); ela fica `proposta` e **não é recuperada** até o operador confirmar (`decide_memory`, com revisão). Só o operador cria memória confirmada direta (`remember`), rejeita ou esquece (`forget`, exclusão definitiva). Agentes não confirmam nada.

## Retenção, correção e exclusão

`purge(empresa)` (varredura do backend): documentos com validade vencida viram `expirado` e saem do índice; memórias vencidas viram `expirada`; documentos revogados/expirados/substituídos há mais de 90 dias são **apagados** (conteúdo substituído por tombstone). `revoke` remove do índice na hora; `delete` apaga o conteúdo e o índice imediatamente. Importar/corrigir = `publish`; só o operador gerencia.

## Limites

Sem interface (T15/T16 não cobrem conhecimento) e sem ligação automática ao runtime; importação é por conteúdo de texto (sem leitura de arquivos/PDF). A busca é lexical (FTS), sem embeddings. O texto apagado pode permanecer em páginas livres do arquivo SQLite até uma compactação (`VACUUM`), e backups anteriores guardam o que existia: a política de backup é da T30. Validado com corpus sintético de duas empresas, não com documentos reais.
