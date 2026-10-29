# Conectar um sistema pela conversa (C7)

Entrega da issue [#107](https://github.com/rafaelmaiabr/assistente/issues/107), parte do acompanhamento [#100](https://github.com/rafaelmaiabr/assistente/issues/100): `team/connectors/conversation.py` (planejador de integrações por conversa), a ferramenta `solicitar_integracao` (`assistant_core.py`), as mensagens do chat (`web_server.py`) e os cartões da tela (`frontend/src/IntegrationCards.jsx`, `integrationModel.js`). Usa a camada de conectores e o painel ([TEAM-CONNECTORS.md](TEAM-CONNECTORS.md)).

> **Estado real:** o fluxo funciona de ponta a ponta com **email (IMAP e SMTP)**, os conectores instalados hoje, validado em navegador com um servidor IMAP falso. **Não há conector de ERP** (o Bling é a [#104](https://github.com/rafaelmaiabr/assistente/issues/104)): pedir "conectar ao Bling" mostra um cartão dizendo que não há conector instalado, sem prometer nada. O caminho de **autorização no navegador (OAuth)** existe e foi testado só com um adaptador falso, porque nenhum conector instalado o usa ainda. Vale para a conversa **local** (o chat não existe no acesso remoto).
>
> **O chat lê a caixa de empresa só em alguns casos** ([#117](https://github.com/rafaelmaiabr/assistente/issues/117), [CHAT-EMAIL-CONEXOES.md](CHAT-EMAIL-CONEXOES.md)). O cartão cria e **testa** uma conexão da **empresa**. O chat passa a ler por ela, pelo gateway e somente leitura, **quando não há email pessoal configurado e ela é a única caixa de empresa utilizável**. Com o email pessoal configurado, ele continua sendo o que o chat lê; com várias caixas de empresa, nenhuma é escolhida. Nesses dois casos o cartão mostra um aviso e o pedido original **não é retomado**.

## O fluxo

1. A pessoa pede ("conectar ao imap", ou algo que exige um conector que ainda não está configurado, por texto ou voz).
2. Duas portas levam ao mesmo cartão: a frase curta "conectar|integrar|ligar <sistema>" cujo sistema é um conector instalado abre o cartão **sem modelo** (um modelo pequeno pode não chamar a ferramenta); e a ferramenta `solicitar_integracao(fornecedor, capacidade)`, que o modelo chama. Frases sobre "email/caixa" continuam indo ao formulário de email pessoal que já existia.
3. O **backend decide** o cartão e verifica, **por empresa**, se a integração já existe e funciona:
   - **Nova:** botão **Conectar**.
   - **Existente** (configurada, credencial ativa, ativa **e testada com sucesso**): **Usar esta conexão** e **Reconfigurar**.
   - **Quebrada** (nunca testada, teste falhou, credencial revogada, desativada, sem senha): **Reconectar**, com o motivo.
   - **Sem conector instalado**, **vários possíveis** (a pessoa escolhe), **sem empresa** ou **atendimento persistente desligado**: um cartão que só explica.
4. Um clique abre o **formulário seguro**, gerado do descritor do driver (campos, tipos, ajuda). O foco vai para o primeiro campo. Senha em `type=password`, sem autocompletar, limpa depois de enviar; ao reconfigurar, em branco mantém a atual.
5. **Conectar e testar:** cria ou atualiza a conexão (cofre, validação do descritor, histórico de configuração do painel) e **roda o teste real**. Só então aparece "conectado e testado". Se o teste falha, a conexão fica e o próximo envio **reconfigura** (não duplica). O cartão é atualizado depois de cada tentativa, para as linhas dizerem o que é verdade.
6. **Autorização no navegador:** para um driver com `auth.type = "oauth"` o formulário mostra os passos do descritor (`steps`, por exemplo criar o aplicativo no portal do provedor e a URL de retorno), salva as configurações e oferece **Autorizar no navegador**. A página abre no navegador (no aplicativo desktop, pelo navegador do sistema, https apenas) e também aparece como link; há estado de espera e **Cancelar**; ao terminar, a conexão é testada de verdade.
7. **Retomada, só quando é verdade:** se a integração nasceu de um pedido e o chat **realmente usa** aquela conexão, depois de conectar a tela envia, como mensagem visível da pessoa, "Conclui a conexão com <sistema>. Pode continuar com o que eu pedi." Quem decide é o servidor (`chat_reads` em `web_server.py`, passado ao `IntegrationSession`); `CHAT_CAPABILITIES` em `conversation.py` continua vazio e só vale quando nenhuma decisão é passada. Quando o chat não lê aquela conexão, a retomada **não acontece** e o resultado e o cartão dizem isso.

## O que o modelo, uma mensagem ou uma página **não** conseguem

- Só o texto "fornecedor" e "capacidade" chega do modelo, e ele só **seleciona entre conectores instalados**. Palavras com instruções embutidas não criam nada (testado).
- **Abrir o formulário, escolher a empresa, enviar dados e autorizar** são mensagens (`integration_open|use|submit|oauth|cancel|pick`) que só o clique produz; cada uma referencia um cartão (`request_id`, 16 hex) criado nesta conversa, que expira em 1 hora, e a empresa tem de estar na lista do próprio cartão. Id inexistente, expirado, de outra conversa ou empresa inventada responde "este cartão expirou".
- No máximo 5 cartões ficam abertos por conversa.
- A **senha só viaja dentro de `integration_submit`**: não volta em nenhum evento ou erro, não fica na sessão nem no histórico de configuração; testes procuram o valor em todos os eventos do WebSocket.
- A voz só dispara o cartão; o assistente avisa que o formulário está na tela e nunca pede nem lê segredos.
- As mensagens `integration_*` são aceitas durante um turno do modelo (o clique não espera a resposta).

## Desktop

O aplicativo nega `window.open`; por isso o preload expõe `openExternal`, e o processo principal só abre, no navegador do sistema, endereços **https sem credenciais** (`externalAuthUrl`, testado), a pedido da janela do chat confiável.

## Limites

- **OAuth** só testado com adaptador falso; nenhum conector instalado o usa, e as particularidades de um provedor (por exemplo porta fixa na URL de retorno) serão tratadas na #104. A ponte do desktop (`openExternal`) foi testada na política, **não** exercitada no aplicativo Electron real.
- Largura de celular não foi conferida visualmente; a conferência foi em navegador de mesa.
- A retomada é uma mensagem automática no chat (visível); se o assistente não estiver pronto, ela espera. Ela só existe para capacidades que o chat usa (hoje nenhuma): ver o estado real acima.
- A frase curta só reconhece verbos "conectar", "integrar" e "ligar"; outros jeitos de pedir dependem do modelo chamar a ferramenta.
- Validado com servidores falsos; nenhuma conta real. Graphify não atualizado.
