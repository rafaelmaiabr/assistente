# Regras de caixa persistentes (#139)

Regras de triagem que sobrevivem à conversa, por **conta** e por **endereço completo ou domínio exato**, criadas **só com a sua escolha** num cartão. Elas alimentam a triagem somente leitura da #138 ([CHAT-TRIAGEM.md](CHAT-TRIAGEM.md)); nada é movido, apagado nem marcado.

## Como se cria

1. Um pedido curto e claro, ou o modelo pela ferramenta `propor_regra_de_caixa`, gera uma **proposta**: "sempre trate os emails de ana@empresa.com como prioridade", "trate as mensagens do domínio empresa.com como prioridade", "marque os emails de loja.com.br como confiáveis". Pedidos assim são atendidos pelo servidor, sem depender do modelo (lição da #150).
2. Aparece o cartão **"Criar regra para [escopo] quando [condição]: [ação], ou aplicar só desta vez?"**, com exemplos do que vale e do que não vale, e três botões: **Criar regra**, **Só desta vez**, **Recusar**.
3. **Só a escolha salva.** "Só desta vez" aplica a regra à triagem daquele momento e **não guarda nada**; "Recusar" descarta. Não nasce regra de um exemplo, de uma limpeza anterior, de silêncio nem de texto dentro de um email (a triagem nunca cria propostas; há teste com um email que tenta criar uma regra).

## O que a regra tem

ID estável (`r-…`), versão e datas, status (`ativa`, `desativada`, `substituida`), conta, escopo, condição, exceções, ação, a regra que substitui (e por quem foi substituída), exemplos positivos e negativos e o **registro da aprovação** (quando, por quem e por onde: cartão ou painel), sem copiar conteúdo de mensagem.

- **Escopo exato**: endereço completo (`ana@empresa.com`) ou domínio exato (`empresa.com`). Sem curinga e **sem subdomínios implícitos** (`mail.empresa.com` não casa com `empresa.com`). Uma regra de uma conta **nunca** vale em outra (a chave da conta é "pessoal" ou a conexão da empresa).
- **Condição verificável**: qualquer mensagem; assunto contém; texto contém; é lista/newsletter (tem cancelamento de inscrição). Até 5 **exceções** (palavras que desligam a regra).
- **Ação**: `prioridade` (a mensagem entra como prioridade, "uma regra que você aprovou marca como prioridade") ou `preservar` (tratada como remetente conhecido: nunca vira suspeita; com indícios de golpe fica em **revisão**). Uma regra **não consegue esconder uma suspeita**: prioridade com indícios continua sinalizada em destaque. `mover_lixeira` **sugere** mover para a Lixeira na triagem (a regra nunca move sozinha: mover exige a proposta e a sua aprovação, em [CHAT-MOVER-EMAIL.md](CHAT-MOVER-EMAIL.md), #140); se uma regra de mover e uma de preservar casarem na mesma mensagem, ela é preservada e marcada como conflito.
- **Verificação de texto**: condição e exceções passam pela mesma verificação da memória (segredo, injeção, permissão, invisíveis).

## Conflito, substituição e a tela

- Mesmo escopo e condição com **outra ação** é **conflito**: nada é decidido sozinho; a resposta diz que já existe e mostra a regra. Mesma ação é duplicata. Substituir exige indicar a regra (ferramenta ou `substitui`); a antiga fica `substituida` (histórico) e só a nova vale.
- O painel **Regras da caixa** lista as regras, **desativa/ativa** (ativar de novo não pode criar conflito), e **edita** o texto da condição, as exceções e a ação (nova versão, aprovação registrada como "painel"). Escopo e conta não se editam: crie outra regra.

## Onde fica

`regras-de-caixa.json` na pasta de dados. Está no `.gitignore` e na lista de nomes privados do empacotamento; a equipe de agentes (`team/`) não o referencia (há teste); `mailbox_rules.py` não registra nada em log.

## Limites

- Testes com mensagens sintéticas, servidor IMAP falso e modelo falso: **nenhum IMAP real e nenhum modelo real** foram usados. Não vi a tela no navegador nem no Electron (rodaram `npm test` e `npm run build`).
- Os pedidos reconhecidos pelo servidor são poucos e por padrão de frase; outras formas vão ao modelo, que pode não chamar a ferramenta. Condições além das quatro acima (por exemplo, por anexo) não existem.
- A regra "preservar" é um voto de confiança seu no remetente: um remetente legítimo comprometido ainda será mostrado em revisão, não como seguro.
- A lista de "remetentes conhecidos" da triagem continua sem cadastro próprio: a regra `preservar` com escopo é o jeito de marcá-los.
