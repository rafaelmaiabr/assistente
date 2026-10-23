# Cenários completos: Secretaria, Suporte, Financeiro e cliente (T28)

Entrega da issue [#43](https://github.com/rafaelmaiabr/assistente/issues/43): `tests/e2e_world.py` (uma empresa fictícia com todos os sistemas externos falsos), `tests/test_team_scenarios.py` (9 cenários ponta a ponta) e uma correção em `team/control.py` encontrada por eles.

Estado: **só testes.** A suíte roda sem rede, sem modelo e sem conta paga, com relógio controlado. Ela prova que os módulos entregues (T03–T25) funcionam juntos; **não prova** que uma caixa de email, um registrador ou um modelo reais se comportem igual (ver "Roteiro real opcional").

## O mundo fictício

Cada sistema externo é um dublê identificado: IMAP e SMTP são servidores locais que falam o protocolo (`tests/imap_fake.py`, `tests/smtp_fake.py`); a web, o DNS e o RDAP são roteirizados (`FakeNetwork`); o registrador é **uma pessoa** que renova à mão (não existe conector, T26); o modelo da triagem é um classificador roteirizado. Endereços usam o domínio reservado `.invalid`. `restart()` reconstrói todos os serviços sobre o mesmo arquivo de banco, simulando a reinicialização do processo.

## Cenários

| Cenário | O que cobre | Teste |
|---|---|---|
| **A** | Cliente → Secretaria (triagem por regra) → Suporte (transferência aceita) → Financeiro por delegação → Suporte → cliente. Diagnóstico com evidência, aprovação, renovação manual, **propagação** antes de **site verificado**, resposta única encadeada ao email original e encerramento só após verificação posterior | `ScenarioA` |
| **B** | Pedido só financeiro: transferência aceita pelo Financeiro, análise sem executar nada, resposta ao cliente e resolução **sem retorno ao Suporte** (um único encaminhamento, sem delegação) | `ScenarioB` |
| Negação | Aprovação negada: nada é renovado, a renovação manual é recusada, o cliente é informado e a demanda **não** é resolvida | `DenialAndTakeover` |
| Pessoa assume | O operador assume: aprovações pendentes são retiradas, rascunhos e propostas dos agentes são recusados, nenhum email sai; ao devolver, os agentes voltam a poder agir | `DenialAndTakeover` |
| Cliente ambíguo | Pedido vago: o modelo (roteirizado) só pede esclarecimento; a pergunta sai com aprovação; a resposta do cliente entra **na mesma demanda** pelos cabeçalhos; a segunda rodada é decidida por regras | `AmbiguousCustomer` |
| Laço e duplicata | A cópia do nosso próprio email, a resposta automática do cliente e o email original reentregue não criam demanda nem resposta; a mesma resposta externa recebida duas vezes é registrada uma vez | `LoopsAndDuplicates` |
| Reinício | Reinício em cada etapa (ingestão, triagem, aceite, delegação, proposta, aprovação, renovação, verificação); o envio aprovado antes da queda sai **uma vez** mesmo despachado de novo depois de outro reinício | `Restarts` |
| Procedência | Toda evidência vem de fonte identificada (`email:`, `renovacao:`); só endereços `.invalid`; a conta com SMTP configurado nunca cai no adaptador simulado | `Provenance` |

Em todos: dono correto a cada passo, uma mensagem de saída por resposta, estados da demanda e isolamento entre empresas.

## O que os cenários encontraram

`OperatorControl.take_over` tentava cancelar **todo** encaminhamento aceito da demanda, inclusive a transferência já concluída (Secretaria → Suporte), que não é cancelável, e falhava com `HandoffRejected` justamente no caso mais comum. Agora só retira o que está em andamento: propostas pendentes e delegações com subtarefa aberta. O cenário "pessoa assume" é o teste de regressão.

## Roteiro real opcional (não executado nesta entrega)

Com contas **autorizadas** e destinatário próprio, na ordem: `scripts/smoke_imap.py` (leitura da caixa, mensagem continua não lida), `scripts/smoke_diagnostics.py <seu-dominio>` (DNS/TLS/HTTP/RDAP), `ImapAccounts.test` e `SmtpAccounts.test` (login sem enviar) e um envio de teste a um endereço seu (roteiro de [TEAM-SMTP.md](TEAM-SMTP.md)). Registrador e modelo reais: não há conector de registrador (T26) e o fluxo de renovação continua manual; a triagem por modelo real foi medida à parte em `scripts/eval_triage.py` ([TEAM-TRIAGE.md](TEAM-TRIAGE.md)).

## Limites

Os dublês seguem o contrato que escrevemos, não o de um provedor real: diferenças de IMAP/SMTP em provedores reais (limites, codificações, filtros de spam, pastas) não são cobertas. Não há interface nesses cenários; a ligação do gateway real ao `TeamApp`, a voz (T31) e o acesso remoto (T32) ficam de fora.
