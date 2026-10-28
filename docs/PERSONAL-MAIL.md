# Habilidade de email no chat (issues #96 e #98)

O chat e a voz têm uma **habilidade de email**: ler a sua caixa (listar, buscar, ler) e **responder com a sua aprovação**. Ela se comporta como as habilidades dos assistentes de desktop: está no catálogo do assistente, é acionada quando o pedido envolve email, aparece na tela como "E-mail · em uso" e, se ainda não foi configurada, abre o formulário de configuração em vez de falhar.

## Configurar pelo chat

1. Diga "configurar caixa de email" (ou clique em **Configurar caixa de email** na tela inicial, ou no selo "E-mail · configurar").
2. Preencha o formulário na tela: email e **senha de aplicativo** (não a senha da conta). O servidor de leitura (IMAP) e o de envio (SMTP) são detectados pelo domínio (Gmail, Outlook/Hotmail, Yahoo, iCloud, Zoho; outros domínios usam `imap.<domínio>` e `smtp.<domínio>`). Em **Avançado** dá para corrigir os servidores ou escolher somente leitura.
3. O servidor testa a leitura e o envio (só o login, nenhum email é enviado). Se qualquer um falhar, **nada é guardado** e a configuração anterior, se havia, continua valendo. Se passar, a habilidade fica ativa na mesma conversa, sem reiniciar.

A senha vai direto do formulário ao servidor local e ao cofre de credenciais do Windows. Ela **não passa pelo modelo, pelo histórico da conversa nem por log**, e o formulário apaga o campo depois de enviar. Nunca digite a senha na caixa de mensagem; se digitar, troque a senha de aplicativo.

Alternativa por terminal: `.venv\Scripts\python.exe scripts\mail_setup.py --host imap.gmail.com --user voce@gmail.com` (use `--read-only` para só ler e `--remove` para esquecer a conta).

## Ler

**Listar, sem modelo (#116).** Frases curtas e diretas são reconhecidas pelo servidor (`mail_intent.py`) e respondidas com a lista da caixa real, sem depender do modelo chamar a ferramenta: "liste meus emails", "mostre os 3 últimos emails", "leia o último email", "tenho email novo?", "qual o último email que recebi?". Também aceita erros comuns de transcrição de voz ("emeio", "imeio", "e mail"). A resposta traz só **remetente, assunto e data** (até 10, padrão 5; "último" mostra 1), dentro de um bloco de texto: um assunto com `[link](https://…)` ou `# título` aparece como texto, nunca como link. Sem caixa configurada, abre o formulário seguro e não mostra a caixa de demonstração. Se a leitura falhar, diz isso sem inventar mensagens. Qualquer frase que não seja um pedido simples de listar ("responda", "apague", "mova para SPAM", "emails do Bruno sobre a reunião") continua indo ao modelo.

Pedidos mais livres: "tem algum email do Bruno?" ou "leia o segundo" dependem do modelo chamar a ferramenta. Somente leitura: o cliente usa `EXAMINE` e `BODY.PEEK`, então nada é marcado como lido, apagado ou movido. Tudo o que vem de um email é marcado como dado não confiável, com texto limitado a 6.000 caracteres e no máximo 10 mensagens por consulta.

## Responder com aprovação

1. Você pede "responda ao email do Bruno dizendo que combinado".
2. O assistente escreve o texto e chama `preparar_resposta`, que **só cria um rascunho**. Ele aparece na tela com **para, assunto e texto completos** e os botões **Aprovar e enviar** e **Descartar**.
3. O email só sai quando você clica. A decisão chega ao servidor como uma mensagem da página; o modelo não tem nenhuma ferramenta que aprove ou envie, e dizer "sim, pode enviar" na conversa não envia nada.

Regras do rascunho:

- O destinatário é sempre quem escreveu o email original; sem cc, bcc nem anexos. O texto do modelo não muda o destinatário.
- Não respondo a email automático, de lista ou meu próprio.
- O rascunho é imutável e a aprovação vale pelo hash do conteúdo exato (destinatário, assunto, texto e Message-ID). Expira em 1 hora e só pode ser enviado uma vez.
- Máximo de 5 rascunhos esperando e 20 envios por 24 horas.
- Envio por SMTP com TLS verificado. Se o servidor recusar antes de receber o conteúdo, nada foi enviado e a tela diz isso. Se a conexão cair depois de enviar o conteúdo, o resultado é **desconhecido**: nunca é reenviado sozinho, e a tela mostra o Message-ID para você conferir na pasta Enviados. "Aceito pelo servidor" não significa entregue nem lido.
- Os rascunhos ficam em `mail-outbox.json` (ignorado pelo Git, sem a senha). Ao recarregar a página, rascunhos ainda válidos voltam à tela.

## Onde ficam os dados

- Servidor, usuário e pastas: `mail-account.json`; rascunhos: `mail-outbox.json`. Ambos na pasta de dados e ignorados pelo Git.
- A senha de aplicativo: somente no cofre do Windows. Nunca em `config.json`, log, resposta do chat ou mensagem de erro.
- Pastas fora da lista autorizada (`INBOX` por padrão) são recusadas sem consultar o servidor.

## Limites

- **Nada disto foi testado com uma caixa real nem com o modelo local chamando as ferramentas** (Ollama/Qwen). Os testes usam servidores IMAP e SMTP falsos e um agente falso. O teste real é de quem é dono da conta e deve ser descrito no PR.
- Modelos pequenos podem escolher mal a ferramenta ou obedecer a um texto malicioso dentro de um email. Por isso o pedido "configurar caixa de email" também é reconhecido pelo servidor sem depender do modelo, e o envio depende só do seu clique. O pior caso de uma injeção é um rascunho indesejado para o remetente do próprio email, que você vê antes de aprovar.
- O atalho de listar **só lista** (remetente, assunto, data): não lê o corpo, não busca por remetente e não resume. Depois dele, o modelo não "vê" a lista que foi mostrada; um pedido como "leia o primeiro" faz o modelo listar de novo. O atalho não atende "mover", "apagar" ou "marcar como SPAM": a leitura é somente leitura por construção, e esses pedidos vão ao modelo, que pode responder mal (issue sobre o assistente não afirmar o que não consegue).
- Provedores como Outlook de trabalho podem bloquear senha de aplicativo; o teste de conexão avisa e nada é guardado.
- Uma conta por instalação, sem OAuth. Sem novo email, encaminhamento, cc ou anexos.
- A "habilidade" aqui é do chat local. Ela não usa o registro de skills nem o gateway da equipe de agentes (`docs/TEAM-SKILLS.md`, `docs/TEAM-GATEWAY.md`).
- No pacote portable, `mail_setup.py` não é copiado; use o formulário do chat.
