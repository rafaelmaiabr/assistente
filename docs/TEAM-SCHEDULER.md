# Agendamentos, SLA, monitoramento e escalonamento (T29)

Entrega da issue [#44](https://github.com/rafaelmaiabr/assistente/issues/44): `team/scheduler.py` (`ScheduleService`, `SlaService`, `Notifier`, handlers de fila), contratos `schedule` e `notification`, migração `0018`. Usa a fila durável ([TEAM-QUEUE.md](TEAM-QUEUE.md)), entrada de emails ([TEAM-EMAIL-INTAKE.md](TEAM-EMAIL-INTAKE.md)) e diagnóstico ([TEAM-DIAGNOSTICS.md](TEAM-DIAGNOSTICS.md)).

Estado: serviços de backend testados com relógio falso. **Não há processo em segundo plano nem tela**: nada roda sozinho até alguém chamar `tick()` (por exemplo, um laço do aplicativo) e iniciar um worker com os handlers. Sem laços reais na CI.

## Rotinas (opt-in, por empresa)

`ScheduleService.create` (só o operador) exige `opt_in=True` e define: tipo de job, payload, **fuso horário**, frequência (`intervalo` de 5 min a 7 dias, `diaria` ou `semanal` com `HH:MM`), política de **catch-up** (`uma_vez` ou `pular`) e **orçamento** (`max_runs_per_day`, até 1440). Os tipos permitidos são uma lista fixa: `email_ingest` (polling IMAP), `asset_check` (checagem de ativo), `sla_sweep` e `retention_sweep`. Uma rotina **só enfileira um job**; ela não cria permissões, não aprova nada (muito menos algo financeiro), não carrega credenciais (payload sem segredos) e não executa nada sem um worker com handler daquele tipo, que mantém as próprias regras. Tipos como `agent_run`, envio de mensagem ou pagamento são recusados. Validadores opcionais (`validators`) rejeitam payloads que apontam para ativo ou caixa inexistentes.

**Fusos:** `UTC`, deslocamento fixo (`UTC-03:00`) ou nome IANA (`America/Sao_Paulo`). No Windows os nomes IANA exigem o pacote `tzdata`, agora em `requirements.txt` e na CI. Horário local inexistente (início do horário de verão) executa logo após a lacuna, uma vez; horário repetido (fim do horário de verão) executa só na primeira ocorrência.

## O laço (`tick`)

Para cada rotina ativa e vencida: calcula as ocorrências vencidas; se a última está dentro da tolerância (2 min) executa; se houve parada (**catch-up**), `uma_vez` executa só a última ocorrência atrasada e `pular` não executa nenhuma, e as demais são contadas em `catchup_skipped`. Depois enfileira o job com chave `schedule:<id>:<ocorrência>` (idempotente), avança `next_run_at` com verificação de revisão e atualiza os contadores.

- **Duas instâncias:** ambas podem rodar `tick`; a chave de idempotência evita job duplicado e só uma instância avança a rotina (`outra_instancia` para a outra).
- **Sem sobreposição:** se o job anterior ainda não terminou, a ocorrência é pulada (`sobreposicao`); após 3 seguidas há um aviso.
- **Orçamento:** passado o limite diário (no dia do fuso da rotina) as ocorrências são puladas e um único aviso por dia acumula contagem.
- **Falhas:** 3 jobs seguidos falhos/esgotados **pausam a rotina** com motivo visível e aviso de ação, em vez de insistir.
- **Visibilidade e controle:** `set_state` (ativo, pausado, desativado) com motivo; retomar continua da próxima ocorrência futura, sem repetir o período pausado; `reschedule` muda fuso, horário, dia ou intervalo recalculando a próxima execução; `list_schedules` mostra estado, motivo, próxima/última execução e contadores.

## SLA e escalonamento

`SlaService.sweep(empresa)` olha encaminhamentos propostos sem aceite (30 min), aprovações pendentes (240 min) e demandas recebidas sem responsável (120 min), configuráveis. Cada item tem **no máximo 2 níveis** de escalonamento (o nível n vale a partir de n × prazo), um por varredura, registrados em `sla_escalations` (idempotente). O escalonamento é uma **notificação ao operador**; não redistribui trabalho nem decide nada, e depois do último nível o item fica em silêncio.

## Notificações com pouco ruído

`Notifier.notify` junta repetições da mesma chave dentro de 6 h em um único registro com contador; depois do prazo, ou de lida, cria um novo. As mensagens são limpas (sem raciocínio do modelo) e limitadas a 1000 caracteres. `list` e `mark_read` são do operador da empresa.

## Handlers de fila

`asset_check_handler`: diagnostica o ativo (T23) e **notifica só quando a categoria muda** (primeiro `site_ok` é silencioso; piora vira aviso de ação; recuperação vira informação). `email_ingest_handler`: chama a ingestão manual da T21; erro ou parada no meio vira falha do job (e, repetida, pausa da rotina).

## Validação

`tests/test_team_scheduler.py` (19 testes, relógio falso): ocorrências por intervalo/dia/semana, mudança de fuso e horário de verão (início e fim, com `tzdata`), fusos inválidos; opt-in, tipos permitidos, segredos, validadores e permissões; execução idempotente; parada de 3 dias com `uma_vez` e `pular`; job lento e sobreposição; orçamento e reinício no dia seguinte; **duas instâncias em threads**; pausa/retomada/desativação e pausa automática; reagendamento; rotina sem autoridade; SLA em níveis, idempotência, silêncio após o limite e isolamento; notificações; handlers.

## Limites

Sem processo residente nem interface; o intervalo é ancorado na última ocorrência; precisão de cerca de um minuto (depende de quem chama `tick`); `sla_sweep` e `retention_sweep` ainda não têm handler de fila (só o agendamento existe); não há feriados nem janelas de silêncio; as notificações ficam no banco, sem envio por email ou push.
