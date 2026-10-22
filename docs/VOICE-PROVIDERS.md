# Perfis de voz

Entrega [#12](https://github.com/rafaelmaiabr/assistente/issues/12). No chat web ou desktop, abra **Configurar voz**. Abrir o painel desliga o microfone. Selecione um perfil, ajuste nome e velocidade, salve e use **Ouvir prévia**. Marque **Usar este perfil nas respostas** e salve para ativar. As mudanças valem para a próxima síntese; uma resposta já em andamento pode usar o perfil anterior. A seleção é compartilhada entre as abas desta instalação.

Cada perfil guarda seu provedor, voz e parâmetros. **Duplicar perfil** permite ter, por exemplo, Dora normal e Dora lenta. O limite é de 20 perfis. As preferências persistem em `voice-settings.json` na pasta de dados, separadas de `config.json`. O arquivo não contém chaves e é ignorado pelo Git. No checkout, a pasta de dados é a raiz; no portable, `%APPDATA%/assistente-local/data`, salvo override `ASSISTENTE_DATA_DIR`.

## Opções disponíveis

| Provedor | Vozes / uso | Preparação |
|---|---|---|
| Windows SAPI | Voz portuguesa instalada, com ajuste de velocidade; alternativa inicial | Sem download adicional |
| Kokoro ONNX | Dora (`pf_dora`), Alex (`pm_alex`) e Santa (`pm_santa`), português brasileiro | **Preparar / verificar Kokoro**, aproximadamente 354 MB, uma vez |
| ElevenLabs | Voice ID da biblioteca do usuário; Multilingual v2, Flash v2.5 ou Turbo v2.5; velocidade, estabilidade e similaridade por perfil | Chave no cofre Windows, Voice ID e créditos disponíveis; ativação explícita |

Kokoro é um modelo de 82 milhões de parâmetros com pesos Apache 2.0; o runtime `kokoro-onnx` usa licença MIT e depende de eSpeak NG/phonemizer, com suas próprias licenças. O projeto fixa o runtime 0.6.1. Os dois arquivos oficiais da release `model-files-v1.1` são verificados por tamanho e SHA-256 antes da instalação em `.models/kokoro`. A síntese força `CPUExecutionProvider`, com quatro threads, e não usa a GPU do Ollama. Preparar exige internet; sintetizar depois é offline. Nada baixa automaticamente ao selecionar um perfil. Falhas de voz preservam a resposta em texto, sem trocar silenciosamente para um serviço pago.

Para atualizar um checkout existente, encerre o aplicativo pelo × do avatar, encerre um servidor web anterior se necessário e reabra `iniciar-desktop.cmd`. O runtime verifica as novas dependências. O backend agora anuncia protocolo 3 para impedir reutilização de servidor antigo. O portable anterior precisa ser recompilado; um executável antigo não recebe o código do checkout automaticamente.

## Chave segura e prática

No perfil ElevenLabs, cole a chave no campo protegido e clique **Salvar chave**. Ela vai ao **Gerenciador de Credenciais do Windows**, vinculada ao usuário atual e à pasta de dados. A interface limpa o campo e recebe apenas o estado “configurada”, sem recuperar o valor. **Remover chave** apaga a entrada do cofre. A chave é compartilhada entre os perfis ElevenLabs da mesma instalação. Sem acesso ao cofre, a operação falha; não há fallback para arquivo em texto. Não coloque chaves em `config.json`, `voice-settings.json`, Git, URL ou conversa com o assistente.

As rotas de configuração aceitam somente origem exata do servidor local e JSON. O frontend transmite a chave apenas ao backend em loopback para gravá-la no Windows. Não há armazenamento em localStorage. Este é um aplicativo local de usuário único: outros processos executando como o mesmo usuário Windows podem acessar suas credenciais. O cofre não pretende isolar aplicativos comprometidos dentro da mesma conta.

A prévia ElevenLabs pede autorização explícita de consumo de créditos. Ativar e salvar o perfil externo autoriza enviar o texto das próximas respostas ao serviço enquanto **Texto e áudio** estiver selecionado. A opção **Somente texto** não chama TTS. As chamadas usam apenas HTTPS para `api.elevenlabs.io`, sem endpoint editável, redirecionamento ou repetição automática. Apenas o texto da síntese é enviado; este adaptador não envia microfone ou histórico inteiro. Retenção e tratamento no provedor seguem as condições da sua conta; não prometemos retenção zero.

## Qualidade e limites da validação

As três vozes Kokoro geraram WAVs de aproximadamente sete segundos neste computador. A medição inicial foi 21,8 s para Dora, incluindo carregamento, 10,37 s para Alex e 8,02 s para Santa com o modelo em memória. Não é um benchmark controlado; carga e hardware alteram os tempos. A preferência sonora precisa ser avaliada ouvindo as prévias. A síntese ainda entrega o WAV completo; não há streaming, sincronização labial ou clonagem nesta entrega. Voz neural não garante diálogo sem pausas.

Os testes automatizados cobrem persistência, rejeição de segredos em JSON, origem/host, consentimento da prévia externa, remoção de chave, sanitização de erros e contrato HTTP ElevenLabs simulado. Síntese real Kokoro foi executada em CPU. Gravação, leitura e remoção de uma credencial descartável foram verificadas no cofre real do Windows. Seleção e persistência de Dora foram verificadas na interface. ElevenLabs não foi chamado com uma chave real nem houve consumo pago. O console LiveKit legado permanece em SAPI; os perfis se aplicam ao navegador e ao Electron.

## Alternativas para evolução

Provavelmente o projeto lembrado como alternativa aberta ao ElevenLabs é [Chatterbox, da Resemble AI](https://github.com/resemble-ai/chatterbox), com variante multilíngue e clonagem. [Coqui XTTS-v2](https://huggingface.co/coqui/XTTS-v2) também é conhecido nesse espaço; confira a licença dos pesos antes de escolher. [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) oferece modelos de síntese, design e clonagem. Esses motores precisam de avaliação própria de instalação, qualidade em português e disputa de memória com a LLM nesta GPU de 6 GB. Nenhum deles está integrado nesta entrega.

Referências: [modelo Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), [vozes brasileiras](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md), [runtime ONNX](https://github.com/thewh1teagle/kokoro-onnx), [API ElevenLabs](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).
