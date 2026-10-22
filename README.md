# Assistente — validação local

> Produto em desenvolvimento: chat com texto e voz no navegador. Avatar desktop é a próxima entrega. Consulte [roadmap](docs/ROADMAP.md) e [fluxo de contribuição](CONTRIBUTING.md). Toda implementação começa por uma issue.

Protótipo de um assistente que conversa por texto ou voz e consulta ferramentas MCP. Base: LiveKit Agents, Ollama, faster-whisper e voz SAPI do Windows. Não requer conta LiveKit Cloud nem chave de API paga.

## Experimentar neste computador

**Interface amigável:** abra `iniciar-web.cmd`. Ele compila a interface na primeira execução, inicia o backend em segundo plano e abre **http://127.0.0.1:8765** no navegador. É necessário Node.js 22+ para compilar. Clique em **Ligar microfone**, autorize no navegador e fale; selecione o dispositivo em **Dispositivos de áudio**. **Resposta** alterna entre texto e texto com áudio. Consulte [voz](docs/VOICE.md). Para encerrar o servidor, use `encerrar-web.cmd`.

O chat tem contexto por aba, estados de processamento e execução de ferramenta, bloqueio de envios simultâneos e ação **Nova conversa**. Atualizar a página, fechar a aba ou iniciar outra conversa descarta o contexto. O servidor permanece ativo ao fechar a aba; para execução e encerramento pelo terminal, use `.venv\Scripts\python.exe web_server.py` e Ctrl+C. Detalhes em [chat web](docs/WEB.md).

**Console de voz anterior:**

1. Abra `iniciar.cmd` para voz ou `iniciar-texto.cmd` para texto.
2. Aguarde a inicialização e fale ou digite: **Verifique a caixa de email X**.
3. Use **Ctrl+T** para alternar entre texto e áudio, e **Ctrl+C** para sair.

Use fones para reduzir o retorno do som do alto-falante ao microfone. A permissão de microfone do Windows deve estar habilitada para aplicativos desktop. Para listar dispositivos: `.venv\Scripts\python.exe agent.py console --list-devices`.

O iniciador seleciona explicitamente o microfone físico Intel e a saída Realtek, pelo backend MME, em `config.json` (`input_device` e `output_device`). Isso evita o microfone virtual NVIDIA Broadcast, que estava definido como padrão e apresentou sinal praticamente nulo no diagnóstico. Para usar um headset, altere esses campos para o nome do dispositivo e seu backend ou o ID listado pelo comando acima. Feche e reabra o assistente depois de alterar a configuração.

**A caixa de email é fictícia.** A consulta passa por um servidor MCP real, iniciado como subprocesso, mas retorna dois emails de demonstração. Nenhuma conta de email real está conectada.

## Configuração

Edite `config.json` para selecionar outro modelo já instalado no Ollama. O padrão é `qwen2.5:7b`, encontrado neste computador. O iniciador usa uma instância Ollama em `127.0.0.1:11435`, com modelos em `%USERPROFILE%\.ollama\models`, sem alterar variáveis globais. O processo Ollama permanece ativo depois de fechar o assistente.

O Whisper `base` transcreve português na CPU, reservando a GPU para a LLM. A síntese usa a voz portuguesa do Windows quando disponível; caso contrário, usa a voz padrão instalada. Downloads iniciais exigem internet; inferência e demonstração MCP são locais, sem cobrança de API (há consumo de recursos e energia).

`mcp_servers` aceita servidores HTTP sem autenticação, por exemplo `[{"url":"http://127.0.0.1:8000/mcp"}]`. Conecte apenas servidores conhecidos. OAuth e contas reais não estão implementados nesta validação.

## Escopo

- Implementado: chat React no navegador com Ollama/MCP reais e contexto da sessão; voz/texto no console; detecção de fala com Silero; LLM configurável.
- Ainda não implementado: avatar desktop, persistência de conversas, integração de email real e roteamento automático entre LLMs.
- A fluidez, interrupções e latência precisam ser avaliadas falando no microfone. A síntese Windows e transcrição por trechos priorizam simplicidade e custo zero, não qualidade final.

## Reproduzir a instalação no Windows

Instalação assistida: `powershell -ExecutionPolicy Bypass -File .\setup.ps1`. Acrescente `-Graphify` para instalar e gerar o mapa de código para agentes. O script preserva `config.json` existente. A aplicação usa os valores de `config.example.json` quando não há configuração local; overrides locais podem ser parciais. Consulte [Graphify](docs/GRAPHIFY.md).

Requer Python 3.13, uv e Ollama. Execute na pasta do projeto:

```powershell
uv venv --python 3.13 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
Copy-Item config.example.json config.json
```

Instale o modelo configurado no diretório usado pelo iniciador. O primeiro uso do Whisper baixa aproximadamente 150 MB para `.models`. As versões instaladas estão fixadas em `requirements.txt`. O comando de console Python funciona na versão fixada, embora esteja marcado como legado pelo LiveKit; numa evolução do produto, migrar para a CLI atual.

## Verificação técnica

Chat web: `python -m unittest discover -s tests -v` valida configuração, origem/host, concorrência, isolamento, erros e encerramento. `npm run build --prefix frontend` compila a UI. Com o servidor e Ollama ativos, `.venv\Scripts\python.exe scripts/smoke_web.py` valida chamada MCP real e uma segunda pergunta com contexto. Este último teste não roda na CI, pois requer modelo local.

Verificado neste computador: síntese Windows, transcrição da frase sintetizada, emissão de áudio pelo adaptador LiveKit e consulta real ao MCP demonstrativo pelo Qwen. O turno com ferramenta levou aproximadamente 6,3 segundos com o modelo carregado. O console em texto também respondeu corretamente. Isso não mede a latência ponta a ponta do microfone nem valida interrupções em ambiente real.

Execute `.venv\Scripts\python.exe verificar.py` com Ollama ativo na porta 11435. O script sintetiza uma frase sem tocá-la, transcreve o áudio, testa o adaptador de síntese LiveKit e solicita uma consulta MCP pelo modelo. O histórico é salvo em `validacao.json`.

Referências: [LiveKit Agents](https://docs.livekit.io/agents/), [starter Python oficial](https://github.com/livekit-examples/agent-starter-python), [MCP no LiveKit](https://docs.livekit.io/agents/logic/tools/mcp/). Este protótipo usa o SDK diretamente e adapta a voz para execução local no Windows.
