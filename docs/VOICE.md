# Voz no navegador — issue #4

Abra `iniciar-web.cmd`, clique em **Ligar microfone** e autorize a captura. Em **Dispositivos de áudio**, selecione o microfone físico. O indicador **Captura ativa** e o medidor mostram quando há captura; **Silenciar microfone** encerra as tracks e descarta a fala ainda não enviada. Nada é capturado ao abrir ou reconectar a página.

Fale e faça uma pausa de cerca de um segundo. O navegador agrupa a fala em trechos de no máximo 20 segundos e envia WAV mono de 16 kHz ao backend local. O Whisper transcreve na CPU, o mesmo agente processa a mensagem e o Windows SAPI gera o áudio. Os estados de transcrição, pensamento, ferramentas e preparação de áudio aparecem na interface. Durante uma resposta em processamento a captura pode continuar ligada, mas novos trechos não são enviados; aguarde para continuar.

**Resposta** permite escolher **Somente texto** ou **Texto e áudio**, independentemente do microfone. A reprodução pode ser interrompida pelo botão **Parar áudio** ou por nova fala quando a captura está ativa. Isso para a reprodução, sem desfazer ferramentas já executadas. O player permite reproduzir a última resposta se o navegador bloquear autoplay.

Use Chrome/Edge atualizado em localhost ou o aplicativo desktop. A captura exige contexto seguro e permissão do navegador. Se o navegador embutido não mostrar a permissão, a ativação expira com orientação. A seleção da saída depende do suporte a `setSinkId` e das permissões do navegador. Use fones para reduzir eco; o navegador solicita cancelamento de eco e redução de ruído.

## Transporte e limites

WebRTC LiveKit foi avaliado: requer servidor RTC, tokens e distribuição de áudio. Para esta validação em uma máquina usamos o WebSocket existente e mantemos LiveKit AgentSession como orquestrador. Não é uma implementação speech-to-speech nem uma chamada WebRTC. A latência inclui esperar a pausa, transcrever e sintetizar a resposta completa.

O backend aceita até 30 segundos de PCM validado por mensagem. A síntese aceita até 4.000 caracteres e, se falhar, preserva a resposta em texto. O áudio recebido fica em memória; SAPI usa um WAV temporário removido após a conversão. Nenhum áudio ou transcrição é publicado no Git. Ao recarregar, desconectar ou iniciar nova conversa, captura e reprodução são encerradas.

A conexão com a LLM permite até 90 segundos para a primeira resposta, para acomodar a carga inicial do modelo Ollama. O limite total de geração é de dois minutos. A primeira pergunta pode demorar mais quando o modelo ainda não está na memória.

## Verificar

`npm test --prefix frontend` testa segmentação, interrupção, silêncio, limite de duração, permissão ausente/negada, cancelamento durante permissão e liberação tardia. `python -m unittest discover -s tests -v` valida formato, tamanho, transcrição, resposta em áudio e recuperação após falha de síntese. Com Ollama e servidor ativos: `.venv\Scripts\python.exe scripts/smoke_voice.py` gera uma frase sintética local e verifica WAV → Whisper → agente → MCP → texto e WAV de resposta. Esse teste não substitui avaliar voz humana e acústica real.
