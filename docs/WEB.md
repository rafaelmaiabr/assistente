# Chat no navegador — issue #3

## Executar

Após `setup.ps1`, abra `iniciar-web.cmd`. Requer Node.js 22+ na primeira compilação, Ollama e o modelo de `config.json`. A interface e o backend são servidos na mesma origem: `http://127.0.0.1:8765`. Sem LiveKit Cloud, servidor RTC ou chave paga para este modo texto.

Alternativa manual:

```powershell
npm ci --prefix frontend
npm run build --prefix frontend
.venv\Scripts\python.exe web_server.py
```

Abra o endereço no navegador. Ctrl+C encerra o servidor manual. O iniciador automático mantém o servidor em segundo plano; fechar a aba encerra somente a sessão. O encerramento pelo aplicativo desktop pertence à entrega de empacotamento. Após modificar Python, encerre e reinicie o servidor. Após modificar React, compile novamente e atualize a aba (isso descarta a conversa).

## Arquitetura

- `assistant_core.py`: fábrica do agente compartilhada pelo console e navegador, com Ollama e MCP.
- `browser_agent.py`: `AgentSession` sem áudio; traduz eventos reais de mensagens, ferramentas e erros. Nenhum Whisper, SAPI ou dispositivo de microfone é inicializado.
- `web_server.py`: servidor aiohttp em loopback, arquivos estáticos e WebSocket `/ws`.
- `frontend/src/`: React com mensagens em texto simples, controle de envio e indicação de estados.

Cada WebSocket possui uma AgentSession e subprocesso MCP próprios. O histórico só existe na memória do processo/aba; fechar a conexão cancela o turno e fecha o agente. Nova conversa abre outra conexão, sem reaproveitar o histórico anterior. São permitidas até quatro sessões, cem mensagens por sessão e oito mil caracteres por mensagem. Há timeout de início (30 s) e de resposta (120 s).

O servidor aceita somente Host localhost/127.0.0.1 na porta configurada e WebSockets com Origin HTTP igual ao Host. Não há CORS aberto nem interface de comandos de sistema. Conteúdo do modelo é renderizado como texto, sem HTML. Esse é um aplicativo para um usuário local: não publicar esse servidor na rede sem implementar autenticação e autorização apropriadas.

O protocolo contém eventos `session`, `status`, `message`, `tool`, `error` e `rejected`. O cliente só envia `{ "type": "message", "text": "..." }`. Um segundo envio durante o turno é recusado também no servidor. Os estados de ferramenta vêm de `tool_execution_updated`, não de temporizadores visuais. A resposta aparece quando o LiveKit confirma a mensagem completa; streaming de tokens não faz parte desta entrega.

## Validação

- Testes automatizados sem LLM: configuração, origem/host, mensagens inválidas, concorrência, falha recuperável, isolamento de abas, timeout e desconexão durante geração.
- Teste de integração opcional: `scripts/smoke_web.py`, com modelo real e caixa MCP fictícia, verifica estados de execução e continuidade do contexto.
- Validação visual no navegador: entrada, envio, resposta MCP e segunda pergunta. O microfone permanece desligado.

Voz na interface, seleção de dispositivos e transcrição estão descritas em [VOICE.md](VOICE.md). O protocolo também aceita `{ "type": "audio", "wav": "base64", "speak": true }`; respostas podem incluir eventos `audio` e `warning` e estados `transcribing`/`synthesizing`. Texto continua aceitando a opção `speak`. Avatar e janela sempre no topo pertencem à issue #5.

## Atendimento persistente (opcional)

O menu **Atendimento persistente** liga o armazenamento local de demandas da equipe (padrão: desligado; o chat continua efêmero). Detalhes e limites em [TEAM-PERSISTENCE.md](TEAM-PERSISTENCE.md). Endpoints locais: `GET /api/persistence`, `POST /api/persistence/enable|disable`, com a mesma verificação de origem das rotas de voz.
