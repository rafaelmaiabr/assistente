# Entregas

Cada etapa começa em uma issue e termina em uma entrega verificável. Uma etapa não depende de finalizar todo o produto.

| Etapa | Entrega | Aceite principal |
|---|---|---|
| 0 — [#1](https://github.com/rafaelmaiabr/assistente/issues/1) | Baseline público | Protótipo documentado sem dados locais publicados |
| 1 — [#2](https://github.com/rafaelmaiabr/assistente/issues/2) | Base reproduzível e Graphify | Configuração local separada, bootstrap, mapa consultável e instruções para agentes |
| 2 — [#3](https://github.com/rafaelmaiabr/assistente/issues/3) | Chat no navegador | Enviar texto, receber resposta real do Ollama via backend e executar MCP; erros e processamento visíveis |
| 3 — [#4](https://github.com/rafaelmaiabr/assistente/issues/4) | Voz na interface | Escolher dispositivos, ligar/desligar microfone, transcrição e estados reais; sem captura antes da ação do usuário |
| 4 — [#5](https://github.com/rafaelmaiabr/assistente/issues/5) | Avatar desktop | Janela Electron arrastável e sempre no topo; estados sincronizados; abrir chat e silenciar |
| 5 — [#6](https://github.com/rafaelmaiabr/assistente/issues/6) | Distribuição Windows | Iniciador amigável, diagnóstico e instruções de instalação; validação de reinício/reconexão |
| 6 | Modelos e integrações | Roteamento entre modelos e email real autorizado, cada integração com issue própria |

## Direção técnica

Preservar Python/LiveKit como camada de agente, Ollama como LLM local e MCP como padrão de ferramentas. Interface React reaproveitada no navegador e no Electron. Avatar inicial 2D, sem sincronização labial. A janela sobre outros aplicativos é responsabilidade do Electron, não de uma aba comum.

O frontend não recebe segredos nem executa comandos arbitrários. No desktop, usar isolamento de contexto e uma API IPC restrita. Definir transporte e ciclo de sessão na issue do chat antes da implementação. Para voz, avaliar LiveKit WebRTC local versus transporte local explícito; não introduzir dependência obrigatória de nuvem.
