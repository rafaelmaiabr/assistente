# Primeira verificação de um domínio

Use quando o cliente informar que o site não funciona. Esta é uma **instrução**, não uma ferramenta: ela diz em que ordem olhar e quando parar.

## Passos

1. Confirme o domínio com o cliente. Sem domínio claro, peça-o antes de qualquer consulta.
2. Consulte o **registro** do domínio (`consultar-registro`): registrador, situação e data de vencimento. Guarde o resultado como evidência separada.
3. Consulte o **DNS** (`verificar-dns`). Compare os servidores de nomes com os do registro.
4. Escreva a hipótese **só com o que as evidências mostram**. Se uma consulta falhar, diga que não foi possível verificar; nunca conclua que o domínio venceu sem evidência.

## Quando você não tem como continuar (lacuna de capacidade)

Pare e **declare a lacuna** em vez de improvisar quando:

- a consulta de registro não cobre aquele tipo de domínio ou registrador (por exemplo, o resultado vem sem dados do registrador);
- o próximo passo exigiria acessar a conta do cliente no registrador, ou uma ferramenta que você não tem;
- a situação não está descrita nestes passos.

Ao declarar, informe: o que o cliente pediu, o que você já tentou (quais consultas e o resultado de cada uma) e se falta **uma instrução** ou **uma ferramenta**. Diga ao cliente apenas que o caso será analisado por uma pessoa da equipe. A demanda fica pausada até uma pessoa decidir se vamos criar uma skill, criar uma ferramenta ou não atender esse tipo de pedido.

## O que nunca fazer

- Não use credenciais do cliente nem peça senhas.
- Não renove, transfira nem altere o domínio ou o DNS; esta skill só consulta.
- Não prometa prazo nem solução antes da decisão da pessoa.
