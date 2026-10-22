# Assistente — validação local

> Produto em desenvolvimento: chat no navegador e avatar desktop estão planejados, ainda não implementados neste baseline. Consulte [roadmap](docs/ROADMAP.md) e [fluxo de contribuição](CONTRIBUTING.md). Toda implementação começa por uma issue.

Protótipo de um assistente que conversa por texto ou voz e consulta ferramentas MCP. Base: LiveKit Agents, Ollama, faster-whisper e voz SAPI do Windows. Não requer conta LiveKit Cloud nem chave de API paga.

## Experimentar neste computador

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

- Implementado: voz/texto no console, detecção de fala com Silero, histórico da sessão, LLM configurável e consulta MCP demonstrativa.
- Ainda não implementado: interface web, persistência de conversas, integração de email real, seleção independente de modalidade de entrada/saída e roteamento automático entre LLMs.
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

Verificado neste computador: síntese Windows, transcrição da frase sintetizada, emissão de áudio pelo adaptador LiveKit e consulta real ao MCP demonstrativo pelo Qwen. O turno com ferramenta levou aproximadamente 6,3 segundos com o modelo carregado. O console em texto também respondeu corretamente. Isso não mede a latência ponta a ponta do microfone nem valida interrupções em ambiente real.

Execute `.venv\Scripts\python.exe verificar.py` com Ollama ativo na porta 11435. O script sintetiza uma frase sem tocá-la, transcreve o áudio, testa o adaptador de síntese LiveKit e solicita uma consulta MCP pelo modelo. O histórico é salvo em `validacao.json`.

Referências: [LiveKit Agents](https://docs.livekit.io/agents/), [starter Python oficial](https://github.com/livekit-examples/agent-starter-python), [MCP no LiveKit](https://docs.livekit.io/agents/logic/tools/mcp/). Este protótipo usa o SDK diretamente e adapta a voz para execução local no Windows.
