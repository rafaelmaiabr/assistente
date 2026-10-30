# Memória curta da pessoa (#137)

O chat pode lembrar poucas coisas sobre a pessoa entre conversas (nome, forma de tratamento, preferências), de forma pequena, curada e sob o controle dela.

## Onde fica e o que a protege

- Arquivo `memoria-pessoal.json` na pasta de dados (`ASSISTENTE_DATA_DIR`, ou a raiz do projeto). Está no `.gitignore` e na lista de nomes privados do empacotamento (`desktop/build.cjs`); a equipe de agentes (`team/`) não o referencia (há teste); o módulo `person_memory.py` não registra nada em log.
- Limites: 1.400 caracteres no total, 200 por anotação, no máximo 10 propostas pendentes (expiram em 7 dias).

## Quem escreve o quê

- **O modelo só propõe.** A ferramenta `propor_memoria` (`adicionar`, `ajustar`, `remover`) cria uma **proposta pendente** e um cartão na tela; nada é gravado. Ela não lê a memória: o texto já está no prompt.
- **Só um clique da pessoa grava.** `Aprovar` aplica a proposta (conferida de novo contra o que a memória é agora); `Recusar` a descarta. Esses botões enviam mensagens (`memory_decision`) que o modelo não consegue produzir.
- A pessoa também **vê, adiciona, edita e apaga** as anotações no painel **Memória** (mensagens `memory_add`, `memory_edit`, `memory_delete`). Vale a mesma verificação.

## Verificação do conteúdo (proposta ou digitado)

Recusa, com o motivo mostrado: texto vazio ou com mais de 200 caracteres; caracteres invisíveis ou de controle (zero-width, bidirecionais); padrão de **injeção** (a mesma heurística da triagem) ou instrução ao assistente; fala de **permissão ou aprovação** ("sem pedir aprovação", "nunca precisa de confirmação"); **segredos** (senha, token, chave, cofre, sequência longa parecida com chave, prefixos como `sk-`/`AKIA`/`eyJ`, número de cartão, endereço `http`); **duplicata exata** (sem diferenciar maiúsculas, acentos e espaços); e o que estouraria o limite, caso em que a resposta traz as anotações atuais para consolidar.

## Congelada na conversa

A memória é lida **uma vez**, quando a conversa começa, e entra no prompt entre a persona e a lista de capacidades, marcada como dado e nunca como instrução, com o identificador de cada anotação para `ajustar`/`remover`. O que for aprovado durante a conversa **só vale na próxima** (o `refresh()` reaproveita o texto congelado). O cartão e a tela dizem isso.

## Limites

- Testes automatizados com modelo e conexão falsos; nada foi medido com o modelo real, então não há evidência de que ele proponha boas anotações nem de que as use bem. Um modelo pequeno pode propor demais ou de menos.
- A verificação é por palavras e padrões: pode recusar um fato legítimo que cite, por exemplo, "senha", e não detecta todo segredo possível. O arquivo não é criptografado; quem tem acesso à pasta de dados o lê.
- A tela não foi vista em navegador nem no Electron (rodaram `npm test` e `npm run build`).
- Compressão de contexto e busca em conversas são a issue [#141](https://github.com/rafaelmaiabr/assistente/issues/141), que exigirá decisão sobre guardar conversas.
