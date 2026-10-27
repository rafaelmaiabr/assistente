# Caixa de email pessoal no chat (issue #96)

O chat e a voz podem **ler** a sua caixa de email: listar os mais recentes, buscar por remetente, assunto ou texto, e ler uma mensagem. É somente leitura: o assistente não envia, responde, apaga, move nem marca como lido (o cliente usa só `EXAMINE` e `BODY.PEEK`).

## Como configurar

1. No provedor (Gmail, Outlook, Zoho etc.), habilite IMAP e crie uma **senha de aplicativo**. Não use a senha da conta.
2. Rode, no projeto:

   ```
   .venv\Scripts\python.exe scripts\mail_setup.py --host imap.gmail.com --user voce@gmail.com --folder INBOX
   ```

   A senha é pedida sem eco. O script testa a conexão e só mantém a configuração se o teste passar. Falha de login apaga o que foi gravado, inclusive uma configuração anterior.
3. Reinicie o servidor (`encerrar-web.cmd` e `iniciar-web.cmd`) e peça no chat: "liste meus últimos emails", "tem algum email do Bruno?", "leia o segundo".

Para esquecer a conta: `scripts\mail_setup.py --remove`.

## Onde ficam os dados

- Servidor, porta, usuário e pastas autorizadas: `mail-account.json` na pasta de dados, ignorado pelo Git.
- A senha de aplicativo: somente no cofre de credenciais do Windows. Nunca em `config.json`, log, resposta do chat ou mensagem de erro.
- Pastas fora da lista autorizada são recusadas sem consultar o servidor. Use `--folder` mais de uma vez para autorizar outras.

## Como o assistente usa

- Sem conta configurada, o chat continua com a caixa **fictícia** e diz isso.
- Com conta, ele recebe três ações (`listar_emails`, `buscar_emails`, `ler_email`) e o prompt diz que a caixa é real e que ele não envia nem apaga.
- Tudo que vem de um email é devolvido com `dados_nao_confiaveis: true`, com texto limitado a 6.000 caracteres e no máximo 10 mensagens por consulta. O prompt manda tratar o conteúdo como dado e nunca seguir ordens escritas dentro de um email. Isso reduz, mas **não elimina**, o risco de injeção de prompt: o modelo local é pequeno e pode obedecer a um texto malicioso. As ações disponíveis são só de leitura, então o pior caso é um resumo errado ou a leitura de outra mensagem da mesma pasta.
- O prompt agora diz o que MCP significa (Model Context Protocol) e proíbe inventar capacidades. Os modelos pequenos ainda erram; isso não é garantia.

## Limites

- Validado com servidor IMAP falso e com o cliente já testado do T18. **Não foi testado com uma caixa real nesta entrega.** O teste real deve ser feito por quem é dono da conta, com `mail_setup.py`.
- Uma conta por instalação; senha de aplicativo, sem OAuth.
- Sem tela de configuração: só o script. No pacote portable o script não é copiado; configure a partir do projeto.
- Não envia nem responde emails. A saída de emails com aprovação existe na equipe de agentes (SMTP, T22), mas não está ligada ao chat.
- O chat não usa a equipe de agentes nem o gateway de ferramentas (ver `docs/TEAM-GATEWAY.md`): esta é uma ligação direta, simples e local.
