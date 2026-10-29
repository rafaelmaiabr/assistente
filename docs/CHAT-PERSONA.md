# Persona e estilo de resposta do chat (#136)

Separa **quem o assistente é** (tom) do **que ele consegue fazer** (ferramentas e avisos de honestidade, #121).

## Persona (arquivo local)

- Arquivo `persona.txt` na pasta de dados (`ASSISTENTE_DATA_DIR`, ou a raiz do projeto se a variável não existir). Está no `.gitignore`; para ficar fora do repositório, aponte `ASSISTENTE_DATA_DIR` para outra pasta. Não é publicado.
- Só descreve **tom**: até 600 caracteres, texto simples. O texto é lido uma vez por conversa.
- **Ausente, vazio, ilegível (não UTF-8, pasta no lugar do arquivo) ou recusado → texto padrão embutido** (`persona.DEFAULT`).
- É recusado **o arquivo inteiro** se uma linha: parecer instrução ao assistente (a mesma heurística de injeção da triagem); afirmar capacidade ("você pode enviar...", "não precisa..."); falar de regras, permissões, aprovação, limites ou ferramentas; citar senha, chave, token, cofre, endereço ou caminho; ou tiver marcação `<...>`. A recusa vai para o log (`persona`) só com o **motivo** (`instrucao`, `capacidade`, `regra`, `segredo`, `grande_demais`), **nunca com o texto**.
- Lugar no prompt: logo depois da parte estável (`BASE`), antes da lista de capacidades reais. Como a lista de capacidades vem das ferramentas registradas, a persona não consegue acrescentar uma.

## Estilos por conversa

- `padrao` (sem texto extra) e `curto` (respostas de no máximo três frases, dizendo que os avisos e regras continuam valendo por inteiro).
- A pessoa escolhe no seletor **Estilo** da tela; a escolha vale para a conversa e volta a `padrao` em conversa nova. O servidor ignora nomes desconhecidos (`Estilo desconhecido.`).
- O estilo é a **última** parte do prompt: trocar de estilo só reescreve o fim, e as ferramentas, a lista de capacidades, os avisos de honestidade e as respostas fixas do servidor (#121) não mudam. A troca só é aceita com o chat ocioso.

## Limites

- Cobertura por testes (persona, prompt, troca de estilo, mensagem do WebSocket, modelo de estado da tela); nada foi medido com o modelo real, então não há evidência de que o modo curto produza respostas mais curtas ou melhores num modelo pequeno.
- A heurística de recusa é conservadora e de palavras: pode recusar uma persona legítima que mencione, por exemplo, "regras", e não substitui revisão do operador. O arquivo não tem editor na tela.
- O seletor não foi verificado em navegador nem no Electron; foram executados `npm test` e `npm run build`.
- Memória curta da pessoa é a issue [#137](https://github.com/rafaelmaiabr/assistente/issues/137), separada.
