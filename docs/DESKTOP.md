# Avatar desktop — issue #5

Com o backend web ativo, abra `iniciar-desktop.cmd`. A primeira execução instala Electron pelo lockfile. O aplicativo abre o chat completo e uma janela menor de avatar, arrastável pela região do personagem/cabeçalho e sempre acima das janelas comuns.

O avatar mostra os estados reais de conexão, captura, transcrição, pensamento, ferramentas e reprodução. **Ligar mic/Silenciar mic** atua na mesma captura do chat; **Abrir chat** restaura a conversa. Fechar a janela do chat apenas a oculta, preservando a sessão e o áudio; o avatar continua indicando a captura. O botão **×** do avatar encerra o aplicativo; **−** minimiza o avatar na barra de tarefas. O microfone começa desligado em toda inicialização.

É um personagem 2D feito em CSS, com animações simples e respeito à preferência de movimento reduzido. Não há sincronização labial ou personagem 3D. Janelas de segurança do Windows e aplicativos em tela cheia exclusiva podem ficar acima de janelas always-on-top.

## Isolamento e comunicação

As duas janelas usam `contextIsolation: true`, `sandbox: true` e `nodeIntegration: false`. A preload expõe somente publicação de estado e comandos enumerados. O processo principal valida remetente, URL e conteúdo. O avatar recebe estado, nível e indicação de microfone — nunca histórico ou credenciais. Navegações, novas janelas, webviews, downloads e pedidos de câmera são bloqueados. A permissão de áudio é restrita à janela do chat local, cuja captura só inicia por ação explícita.

O chat conserva a única AgentSession; o avatar não abre outra conexão MCP nem outra captura. O renderer continua ativo com a janela oculta para permitir conversação pelo avatar. A segunda execução do aplicativo restaura as janelas da instância existente.

## Validação

`npm test --prefix desktop` valida origem, permissões e sanitização. Com o servidor ativo, `desktop\node_modules\electron\dist\electron.exe desktop --smoke-test` verifica propriedades reais de always-on-top, sandbox, isolamento de contexto e ausência de Node no renderer, e encerra.

Inicialização/diagnóstico automáticos do backend, distribuição e encerramento de processos pertencem à issue #6.
