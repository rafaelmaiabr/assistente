# Primeiros passos, operação e atualização da equipe (T36)

Entrega da issue [#51](https://github.com/rafaelmaiabr/assistente/issues/51): o assistente de configuração por modelo (`team/onboarding.py`, `team/templates/`), o diagnóstico da instalação (`team/doctor.py`, `python -m team.doctor`), a lista de arquivos do pacote Windows (`desktop/build.cjs`) e este guia. Nada aqui conecta contas reais, envia mensagens ou aprova algo.

## Começar

1. **Instale e abra o aplicativo** ([DISTRIBUTION.md](DISTRIBUTION.md)). O chat funciona sozinho, sem a equipe.
2. Na seção **Equipe**, clique **Diagnóstico da instalação → Conferir agora**. Cada linha diz o que foi encontrado e, se não estiver bem, **o que fazer**. Resolva os itens "Corrigir" antes de seguir (por exemplo, dependência faltando ou Ollama parado).
3. **Ative o atendimento persistente.** Sem isso a equipe não salva nada. Desligar depois nunca apaga os dados.
4. Clique **Configurar por um modelo**, escolha um modelo, digite o nome da empresa e **Pré-visualizar**. A pré-visualização lista tudo que será criado (departamentos, agentes, capacidades de triagem, quem pode encaminhar para quem e o acesso às demandas que cada agente recebe) e o que **não** está incluído. **Criar equipe** só vale para exatamente aquela pré-visualização: se mudar o modelo ou o nome, confira de novo.
5. Abra uma **demanda de exemplo** na aba Demandas e acompanhe o que a central mostra. Os agentes e os dados de exemplo são fictícios; nenhuma conta é conectada.
6. Só depois, e por sua decisão: cadastre conexões reais (email, etc.) na aba Conexões, instale skills e dê concessões. Cada passo tem sua própria revisão e aprovação.

Modelos incluídos (`team/templates/`, versionados): **Atendimento básico** (Secretaria, Suporte e Financeiro, com encaminhamento entre eles) e **Somente Suporte**. Um modelo é só dados: não pode conter endereços, contas, segredos nem instruções que tentem anular regras ou aprovações (o mesmo filtro dos perfis de agente), e a pré-visualização já recusa o que a criação recusaria. Se algo falhar no meio da criação, a empresa fica **desativada** e a mensagem diz isso, em vez de ficar pela metade sem aviso.

Quer continuar o desenvolvimento do projeto? A próxima entrega elegível é indicada pelo manifesto [team-backlog.json](team-backlog.json) e por [TEAM-IMPLEMENTATION.md](TEAM-IMPLEMENTATION.md): uma issue, uma branch e um PR por vez.

## Operar no dia a dia

- **Demandas** ([TEAM-CASES-CENTER.md](TEAM-CASES-CENTER.md)): fases, aprovações com a ação concreta, intervenção (assumir, pausar, devolver, rejeitar) e reconciliação de resultados incertos.
- **Lacunas de capacidade** ([TEAM-GAPS.md](TEAM-GAPS.md)): quando a equipe não sabe fazer algo, ela para e você decide criar uma skill, criar uma ferramenta ou não atender.
- **Agendamentos e SLA** ([TEAM-SCHEDULER.md](TEAM-SCHEDULER.md)), **auditoria, retenção e backup** ([TEAM-OPS.md](TEAM-OPS.md)).
- **Acesso por outras pessoas** (opcional, desligado por padrão): login com Google, papéis e escopo por departamento em [TEAM-REMOTE.md](TEAM-REMOTE.md).
- **Antes de mudar prompt, skill, modelo ou versão:** `python scripts/run_evals.py` e o [checklist de release](TEAM-RELEASE.md).

## Desligar e reiniciar

- **Fechar o aplicativo** (× no avatar) encerra o que ele iniciou. As demandas, os trabalhos pendentes e as configurações **ficam no banco** e voltam na próxima abertura; efeitos externos com resultado incerto continuam em reconciliação e **nada é reenviado sozinho**. Testado: uma demanda pendente e a identidade do operador sobrevivem ao reinício.
- **Desligar o atendimento persistente** (Equipe) para de salvar; o banco permanece e o chat continua efêmero. Ligar de novo traz tudo de volta (testado).
- Para parar só o servidor web do checkout: `encerrar-web.cmd` ou `scripts/stop-web.ps1`.

## Atualizar o aplicativo

Os dados **não ficam dentro do pacote**: configuração, banco, cofre e ambiente Python ficam na pasta de dados do usuário (normalmente `%APPDATA%\assistente-local\data`; `ASSISTENTE_DATA_DIR` escolhe outra). Instalar uma versão nova, portanto, **não toca** neles.

Ao abrir a versão nova:

1. Se o banco tem migrações pendentes, o aplicativo guarda uma cópia `team.sqlite3.pre-migracao-NNNN.bak` ao lado dele e aplica as migrações, uma por transação.
2. Um banco **mais novo** que o programa (por exemplo, depois de voltar a uma versão antiga) é **recusado sem alteração**, com a orientação de atualizar o aplicativo.
3. As credenciais ficam no cofre do Windows e não dependem do pacote; `config.json` não é sobrescrito.
4. O diagnóstico informa, antes e depois, se há migrações pendentes, se o banco está íntegro e se faltou alguma dependência.

Recomendado antes de atualizar uma instalação com dados reais: um **backup verificado** ([TEAM-OPS.md](TEAM-OPS.md)). Testado com um banco de um esquema anterior: dados, identidade do operador, `config.json` e o estado de ativação ficam intactos, e a cópia `pre-migracao` é criada.

## Restaurar dados

Pare o aplicativo e siga a restauração de [TEAM-OPS.md](TEAM-OPS.md) (`BackupService.restore`): confere manifesto, soma de verificação e integridade, guarda o banco atual como `.pre-restore-<data>` e reconcilia o que estava em andamento. **Segredos não voltam com o backup**: recadastre as credenciais no cofre (e, no acesso remoto, `python scripts/remote_setup.py set-secret`). Para voltar à versão anterior do programa depois de uma migração, restaure a cópia `.pre-migracao-NNNN.bak`.

## Diagnóstico

`python -m team.doctor` (ou o botão na Equipe, só no modo local) confere, **sem migrar nem gravar no banco**: Python, dependências (`aiohttp`, fusos horários, cofre do Windows), pasta de dados gravável, atendimento ligado, banco (migrações aplicadas/pendentes/desconhecidas/alteradas e integridade), backups, cofre, servidor do modelo, configuração do acesso remoto e os modelos de equipe. Cada problema vem com uma ação. Segredos nunca aparecem no relatório (testado). Ele só cria a pasta de dados vazia e um arquivo temporário nela para provar que é gravável.

## O pacote Windows

`desktop/build.cjs` monta o backend a partir de um **inventário de arquivos** em vez de copiar pastas inteiras: dentro de `team/` só entram `.py`, `.sql`, `.json` e `.md`; qualquer outro arquivo (banco, `config.json`, log, áudio, modelo, certificado, chave, `.env`) **interrompe o build** em vez de ser empacotado em silêncio. Entram as 22 migrações, os esquemas, os modelos de equipe, as skills de demonstração e o exemplo de pacote; ficam de fora `tests/`, `evals/`, `scripts/`, o grafo e qualquer dado. O `build-manifest.json` lista as migrações, os modelos e a contagem de arquivos e declara `containsLocalConfig: false` e `containsUserData: false`.

## Limites e integrações ainda não prontas

- **Não existe conector de registrador (#41) nem de ERP/financeiro (#42)**: serão construídos sob demanda, se uma lacuna real decidir por isso.
- **Login com Google e acesso remoto, OAuth Gmail/Microsoft, IMAP, SMTP e Ollama real não foram validados contra os serviços de verdade**: os testes usam provedores falsos. O [checklist de release](TEAM-RELEASE.md) lista cada pendência.
- **PostgreSQL com workers separados (#49) e interoperabilidade A2A (#50) não foram feitos**: o banco é o SQLite local e não há workers fora do processo do aplicativo.
- O **chat continua sendo o assistente local**; ele identifica a demanda e o agente, mas não executa o agente da equipe ([TEAM-CONVERSATION.md](TEAM-CONVERSATION.md)).
- O assistente de configuração só cria a equipe (empresa, departamentos, agentes, capacidades, encaminhamentos e acesso às demandas); não instala skills, não cria conexões e não tem como desfazer: o que ele criou se edita ou se desativa nas seções da Equipe.
- **Não foi gerado nem executado o instalador Windows completo nesta entrega** (isso grava a pasta `release/`): o inventário do pacote foi montado e importado em isolamento, com um banco novo, e o `--smoke-test` do Electron foi executado; o teste manual com microfone e voz reais continua pendente.

## Validação

`tests/test_team_onboarding.py` (19): modelos válidos e sem contas/segredos/endereços, modelos quebrados recusados (referências soltas, campos extras, política remota, perfis que anulam aprovações, endereços, segredos); pré-visualização que não grava e tem hash estável; criação exata do que foi mostrado, sem conexões nem skills; **a equipe criada funciona** (a triagem roteia "meu site está fora do ar" ao Suporte); hash e nome conferidos; falha no meio deixa a empresa desativada; só o dono local; sem rota remota; API e diagnóstico antes da persistência; diagnóstico (instalação nova, migração pendente, banco de versão mais nova sem alteração, migração adulterada e arquivo corrompido, backup, cofre, modelo, acesso remoto, nenhum segredo); atualização a partir de um esquema anterior, reinício com demanda pendente e desligar/ligar a persistência. `desktop/tests/build.test.cjs` (6): migrações, esquemas, modelos e skills no inventário; nada privado ou inesperado; build interrompido por arquivos indevidos; nenhuma importação de `evals`, `scripts` ou `tests`. `frontend/tests/onboarding.test.js` (3). Regressão completa.
