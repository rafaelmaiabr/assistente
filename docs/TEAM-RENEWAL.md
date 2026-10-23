# Fluxo de renovação de domínio com execução manual rastreada (T25)

Entrega da issue [#40](https://github.com/rafaelmaiabr/assistente/issues/40): `team/renewal.py` (`RenewalFlow`) e um ajuste em `team/approvals.py`. Orquestra serviços que já existem: diagnóstico ([TEAM-DIAGNOSTICS.md](TEAM-DIAGNOSTICS.md)), encaminhamentos ([TEAM-HANDOFFS.md](TEAM-HANDOFFS.md)), aprovações ([TEAM-APPROVALS.md](TEAM-APPROVALS.md)), registros financeiros ([TEAM-FINANCE.md](TEAM-FINANCE.md)) e demandas ([TEAM-CASES.md](TEAM-CASES.md)).

Estado: **este sistema não compra nem renova nada.** A renovação é feita por uma pessoa fora do sistema e o operador registra a confirmação. Sem tela, sem ligação ao `TeamApp` e sem conector de registrador (T26). Validado com rede, agentes e registros fictícios; nenhum registrador real foi usado.

## As etapas

1. **Suporte diagnostica** (`diagnose`): roda o diagnóstico somente leitura do domínio (só ativos autorizados) e guarda o resultado como evidência técnica `renovacao:diagnostico` (tipo resultado de ferramenta, com hash).
2. **Suporte delega ao Financeiro** (`open_financial_subtask`): só se o diagnóstico daquele domínio apontar problema de registro (`dominio_expirado`, `dominio_retido`, `suspeita_de_expiracao`, `dns_sem_resposta_dominio_vigente`, `dominio_nao_encontrado`). A demanda continua com o Suporte; o retorno é definido no encaminhamento (`return_to` = dono atual). O texto diz expressamente que nada foi comprado.
3. **Financeiro pede aprovação ou informações** (`propose`): consulta a análise financeira da contraparte (T24). Havendo achado bloqueante (duplicata, moeda divergente, beneficiário alterado…), **não cria aprovação** e devolve `pedir_informacoes` com as perguntas. Sem bloqueio, pede a aprovação da ação `renovar_dominio` com recurso, valor, moeda e `execucao: manual`. A mensagem diz que a renovação "AINDA NAO foi feita".
4. **Pessoa renova e o operador registra** (`record_manual_renewal`): só o operador, só para aprovação **aprovada** daquela demanda, e **valor e moeda iguais ao aprovado** (divergência exige nova aprovação). Data não pode estar no futuro. Consome a aprovação (uso único), registra o efeito como confirmado e grava a evidência `renovacao:recibo` com referência, valor, moeda, data, registrador e a ressalva "confirmação humana não verificada no registrador".
5. **Verificar de novo** (`reverify`): diagnóstico completo e consulta ao registro. Estados: `sem_confirmacao_de_renovacao` (informativo), `resolvido_tecnicamente` (tudo responde), `em_propagacao` (a falha é de DNS/registro e a confirmação tem menos de 24 h) e `falha_persistente` (passou da janela, ou o problema é de HTTP/TLS/servidor, que renovação não explica). Cada verificação vira evidência `renovacao:verificacao`.
6. **Retorno ou assunção**: o Financeiro devolve o resultado ao Suporte (`return_to_support`) para validação técnica; texto que afirme renovação/compra/pagamento sem negação é recusado. Se resta só a cobrança, o Suporte propõe a transferência (`hand_over_billing`) e o Financeiro passa a dono ao **aceitar**.
7. **Encerrar** (`close_if_verified`, só operador): só resolve a demanda quando a verificação mais recente é `resolvido_tecnicamente` e **posterior** à confirmação humana. Negação, expiração, falha ou verificação antiga **nunca** encerram como resolvida (`FlowRejected` com os bloqueios).

`status` mostra etapa, aprovações, encaminhamentos, bloqueios para encerrar, próximo passo e o aviso de execução manual. As etapas vêm dos registros guardados (aprovações, encaminhamentos, evidências com marcador), sem estado novo.

## Sem registrador configurado

O aviso é sempre explícito: "Não há conta de registrador configurada: a renovação é feita manualmente por uma pessoa. Este sistema não comprou nem renovou nada." Mesmo existindo uma conexão `registrador`, o aviso muda apenas para dizer que a execução segue manual até o conector da T26.

## Ajuste em aprovações

Quem pede aprovação passa a poder ser **o dono da demanda ou o agente que executa uma subtarefa delegada em aberto** dessa demanda (antes só o dono), para o Financeiro pedir sem virar dono. Quem pede nunca decide (só o operador aprova) e outro agente sem relação com a demanda continua recebendo `NotFound`. Para consumir uma aprovação, o solicitante precisa ser o dono atual, o operador ou alguém que recebeu subtarefa naquela demanda.

## Validação

`tests/test_team_renewal.py` (12 testes): fluxo completo Suporte→Financeiro→aprovação→renovação manual→propagação→falha persistente→site saudável→retorno→encerramento; aprovação negada; aprovação expirada (tempo esgotado); renovação confirmada com site fora por motivo que não é renovação (503 e TLS); verificação antiga não vale; assunção pelo Financeiro; achados financeiros virando perguntas sem aprovação; diagnóstico exigido (mesmo domínio, problema de registro, ativo autorizado); permissões; valor/moeda/data/uso único; ausência de afirmações de compra; aviso de registrador; isolamento entre empresas.

## Limites

Nenhuma integração com registrador; a renovação é de fato executada por uma pessoa e só **declarada** ao sistema (o RDAP ajuda a conferir a nova expiração, mas não é exigido). A janela de propagação é fixa (24 h) e não há agendamento da reverificação (T29). O Financeiro precisa ter a contraparte e o valor corretos; a análise financeira depende de registros importados (T24). Sem interface.
