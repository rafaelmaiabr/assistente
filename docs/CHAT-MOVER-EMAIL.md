# Mover emails para a Lixeira ou o Spam, com aprovação exata (#140)

Primeira **escrita** no email do projeto, restrita a **mover** mensagens da caixa pessoal para a **Lixeira** (recuperável) ou o **Spam**. Aprovada por você, lote a lote, com a lista exata na tela.

> **Estado de validação:** testada só contra servidores IMAP **falsos** (`tests/imap_fake_rw.py`). **Nenhum servidor real foi usado**: antes de usar numa conta real, é preciso uma conta autorizada pelo operador para um teste controlado. Veja os limites no fim.

## Como se pede

- Frases curtas e claras, atendidas pelo servidor (sem depender do modelo): "mova o email 12 para a lixeira", "mova os emails 12, 15 e 18 para o spam", "mova os suspeitos para o spam". Os números são os `uid` mostrados na listagem e na triagem ([CHAT-TRIAGEM.md](CHAT-TRIAGEM.md)). "Os suspeitos" são exatamente as mensagens que **a última triagem** classificou como `suspeito` (nunca as de "revisão" nem as de prioridade); sem triagem, nada é proposto.
- O modelo tem a ferramenta `propor_mover_emails`, que **só propõe**.

## O que acontece

1. A **proposta** lê os cabeçalhos das mensagens (`BODY.PEEK`, sem marcar como lidas), descobre a pasta de destino e mostra um **cartão com a lista exata** (UID, remetente, assunto), a pasta de destino e uma **conferência (hash)** de tudo o que seria feito. Nada é movido.
2. **Aprovar e mover** envia o hash mostrado no cartão. Sem clique, com outro hash, com a proposta já usada ou vencida (1 hora), **nada é movido**. Cancelar descarta.
3. O servidor move **uma mensagem por vez**. Antes de cada uma confere que a pasta ainda tem o mesmo `UIDVALIDITY` e que o UID ainda é a mensagem aprovada (mesmo `Message-ID`); depois confere que saiu da origem e está no destino. Se qualquer conferência falhar, **para o lote**; um timeout **não é repetido** (o estado fica "desconhecido" e a resposta manda conferir a pasta). Mensagem que já estava no destino é registrada como "já movida" e o lote segue.

## O que nunca acontece

- **`EXPUNGE` geral**: o módulo só tem cinco comandos de escrita (`SELECT`, `UID MOVE`, `UID COPY`, `UID STORE` de `\Deleted` num UID já copiado e verificado, `UID EXPUNGE` desse UID); qualquer outro é recusado no código. O teste prova que o servidor falso nunca recebeu `EXPUNGE`.
- Apagar de vez, esvaziar Lixeira/Spam, criar pastas (se a Lixeira/Spam não existe, a proposta é recusada), mover para outra pasta, marcar como lida.
- Sem `UID MOVE` nem `UIDPLUS` no servidor, **recusa**, em vez de improvisar.
- O cliente de leitura (`team/imap_client.py`) continua sem nenhum comando de escrita (há teste).

## Quem pode

- **Caixa pessoal com envio configurado.** Uma caixa configurada como "somente leitura" **não move** (a escolha é respeitada). 
- **Caixas de empresa continuam só leitura**: elas não têm lado de escrita. Mover numa caixa de empresa exige uma **concessão de escrita** pelo gateway, presa a um caso e a aprovações da equipe; isso **não** faz parte desta entrega (escopo decidido com o operador; fica para uma issue própria).

## Registro

`movimentos-de-email.json` (na pasta de dados; fora do Git e do pacote; a `team/` não o referencia): por mensagem, **antes** (conta, pasta, UIDVALIDITY, UID, Message-ID, destino) e **depois** (resultado e novo UID quando o servidor informa). **Sem remetente, assunto, corpo ou anexo** depois que a proposta é gasta. O módulo não registra nada em log.

## Regras de caixa (#139)

A ação `mover_lixeira` de uma regra passou a valer como **sugestão** na triagem ("uma regra sua sugere mover para a Lixeira"). A regra **nunca move sozinha**: mover continua exigindo a proposta e a sua aprovação.

## Limites

- **Nenhum IMAP real foi testado**; servidores reais variam (nomes de pasta em UTF-7, `COPYUID`, MOVE sem resposta de UID, limites de taxa). O comportamento está verificado contra o falso, que simula: MOVE e UIDPLUS, falha no meio, timeout, `UIDVALIDITY` mudado, mensagem já movida, trocada ou sumida, e um servidor que responde OK mas deixa a mensagem na origem.
- A descoberta da Lixeira/Spam usa o atributo especial (`\Trash`, `\Junk`) e uma lista de nomes comuns; nomes acentuados codificados em UTF-7 podem não ser reconhecidos (a proposta é recusada, nada é criado).
- Até 20 mensagens por proposta. A tela não foi vista em navegador nem no Electron (rodaram `npm test` e `npm run build`).
- Sem a escrita pelo gateway, uma conta de empresa não pode ser movida pelo chat.
