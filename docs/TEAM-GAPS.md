# Lacunas de capacidade: evolução por demanda (T37)

Entrega da issue [#82](https://github.com/rafaelmaiabr/assistente/issues/82): `team/gaps.py` (serviço), tabela `capability_gaps` (migração 0020), consulta da triagem a pedidos já recusados, sinais na central de demandas, rotas `/api/team/companies/{id}/gaps`, bloco "Lacunas de capacidade" no painel e a skill de exemplo `diagnostico-dominio` (instrução em .md).

## Ideia

O projeto não tenta prever todas as integrações. Quando um agente recebe um pedido para o qual **não há instrução (skill em .md) nem ferramenta**, ele **para** em vez de improvisar, registra uma *lacuna de capacidade* e a demanda fica à espera de uma pessoa. Depois de analisar, a pessoa decide:

| Decisão | O que acontece |
|---|---|
| **Criar uma skill** | a demanda continua pausada; a nota registra a decisão e a referência do trabalho (por exemplo, uma issue). Uma skill é só texto, sem código. |
| **Criar uma ferramenta** | igual à anterior; o script ou conector vira uma issue de implementação normal, com os testes e as aprovações do projeto. |
| **Não atender** | as demandas ligadas são **canceladas** com o motivo e uma **memória confirmada** "fora do escopo" é guardada; a triagem a reconhece nas próximas vezes. |

Quando a skill ou a ferramenta passa a existir, a pessoa marca a lacuna como **atendida** e, se quiser, retoma as demandas que esperavam por ela.

Isso vale também para conectores de fornecedor: o registrador (#41) e o ERP (#42) deixam de ser entregas antecipadas e só serão construídos se uma lacuna real decidir por *criar uma ferramenta* para aquele fornecedor.

## Como a lacuna nasce

`GapService.report(ctx, case_id, requested, tried=[…], gap_type, topic)`:

- Quem pode: o **agente responsável atual** da demanda ou o **operador** confiável. Outro agente é recusado; uma demanda encerrada ou de outra empresa também.
- Guarda: o pedido (até 500 caracteres, sem raciocínio interno do modelo e sem segredos), o que foi tentado (até 10 itens de 200 caracteres), o tipo (`instrucao` ou `ferramenta`), o assunto e a lista de demandas ligadas. Nada é executado e nada é concedido.
- Efeito na demanda: ela passa a **pausada** com a lacuna registrada na linha do tempo (evento `case.state-changed` com `gap_id` e nota interna). Uma demanda ainda sem responsável (fila inicial) mantém o estado, porque já espera por uma pessoa, e mostra a lacuna do mesmo jeito.
- A central mostra a fase "Aguardando uma pessoa", o contador `open_gaps` e a lacuna no detalhe; a caixa de atenção do operador lista as lacunas abertas.

### Agrupamento

O assunto (`topic`) é uma chave estável: as primeiras palavras significativas (até 4), sem acento, em minúsculas e em ordem alfabética. O agente pode informar um assunto curto; sem ele, usa-se o pedido. Lacunas abertas com o mesmo assunto na mesma empresa viram **uma só pendência** com a contagem de ocorrências e as demandas ligadas (até 20), e a lista ordena a mais repetida primeiro. O mesmo caso relatando de novo não conta duas vezes.

**Limite:** o agrupamento e o reconhecimento são por **palavras em comum**, não por significado. Dois pedidos iguais escritos com palavras diferentes podem virar duas pendências; informar um assunto curto reduz isso.

## Decisão e retomada (somente o operador)

- `decide(ctx, gap_id, expected_revision, decision, note, follow_up=None)`: sempre com a revisão esperada e uma justificativa (até 1000 caracteres). Uma lacuna já decidida não decide de novo. Na recusa, a memória é criada antes e removida se a gravação falhar, para não sobrar memória sem decisão.
- `fulfill(ctx, gap_id, expected_revision, note, resume=True)`: só para skill ou ferramenta decididas. Retoma as demandas que **ainda estão pausadas e sob a equipe**; uma demanda que o operador assumiu nesse meio-tempo não é tocada.
- Agentes não decidem, não leem a lista e não veem outra empresa (a mesma resposta "não encontrado" do resto da API).

## Triagem reconhece o que já foi recusado

Antes de rotear, a triagem procura uma lacuna `nao_atender` cujo assunto tenha **todas** as palavras presentes na mensagem e cuja memória ainda esteja **confirmada**. Se encontra, não tenta rotear: a demanda vai direto à fila humana com o motivo `fora_do_escopo` (com a lacuna e a memória de origem), para uma pessoa responder. A triagem **não responde ao cliente sozinha**. Esquecer a memória (`forget`) desliga o reconhecimento.

## API e painel

- `GET /gaps?state=`, `POST /gaps` (o operador registra), `POST /gaps/{id}/decide` e `POST /gaps/{id}/fulfill`, com o corpo restrito aos campos conhecidos, a revisão esperada e erros curtos (409 se os dados mudaram).
- O detalhe da demanda mostra o pedido, o que foi tentado, o tipo, a contagem, a decisão e os botões *Criar uma skill*, *Criar uma ferramenta*, *Não atender* (cada um exige justificativa) e *Capacidade pronta: retomar*. A lista de demandas marca as que têm lacuna aberta.

## Exemplo de skill em .md

`team/skills_demo/diagnostico-dominio/` (rótulo de demonstração, como as demais): a instrução manda confirmar o domínio, consultar o **registro** e o **DNS** com o diagnóstico já existente ([TEAM-DIAGNOSTICS.md](TEAM-DIAGNOSTICS.md)), escrever a hipótese só com as evidências e, quando não houver como continuar (registrador não coberto, acesso à conta do cliente, situação não descrita), **declarar a lacuna** e dizer ao cliente apenas que uma pessoa vai analisar. Só consulta: não renova, não altera DNS e não usa credenciais.

## Validação

`tests/test_team_gaps.py` (19): lacuna declarada e demanda parada com evento e nota; central e caixa do operador; apenas o responsável ou o operador; demanda encerrada e outra empresa; agrupamento (mesmo assunto, mesmo caso, assuntos diferentes, ordem); chave do assunto; texto limpo, limites, sem segredo; fila inicial; decisão só do operador, com revisão, justificativa e decisão válida; cada uma das três decisões e seus efeitos; nenhuma memória órfã após conflito; retomada que respeita o controle do operador; lacuna recusada não é "atendida"; isolamento entre empresas; triagem que reconhece o assunto recusado, segue normal para outros pedidos, para de reconhecer depois do `forget` e não vaza para outra empresa. `tests/test_team_gaps_web.py` (3): ciclo completo pela API, erros curtos e isolamento. `frontend/tests/cases.test.js`: regras de decisão do painel.

## Limites

- **O agente não chama isto sozinho ainda.** O runtime atual não executa ações sobre demandas; hoje a lacuna é registrada pelo serviço (código do backend ou operador pela API/painel). Ligar o laço do agente a `report` acompanha a evolução do runtime (T08/T10); a skill de exemplo já descreve o comportamento esperado.
- Nenhum LLM foi usado nos testes; o texto da lacuna é escrito por quem registra e tratado como dado não confiável.
- *Criar uma skill/ferramenta* só **registra a decisão e a referência**: não cria a issue nem o código. Isso continua sendo trabalho normal do projeto, com issue, testes e revisão.
- Reconhecimento por palavras (ver acima); a triagem não responde ao cliente.
- Sem e-mail, conta externa ou credencial envolvidos.
