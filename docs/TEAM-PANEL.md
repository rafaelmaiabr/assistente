# Painel da equipe (T15)

Entrega da issue [#30](https://github.com/rafaelmaiabr/assistente/issues/30): seção **Equipe** na interface (navegador e Electron, o mesmo React), API local `team/web.py` e `team/app.py`. Cobre empresas, departamentos, agentes (com versões), skills, concessões e conexões. A central de demandas, aprovações e mensagens está em [TEAM-CASES-CENTER.md](TEAM-CASES-CENTER.md).

## Como usar

1. Na barra lateral, **Equipe**. Se o atendimento persistente estiver desligado, a tela explica e permite ativá-lo ali mesmo (é opcional; o chat não depende dele).
2. Crie ou escolha a **empresa**: o nome da empresa selecionada fica sempre visível no cabeçalho e todas as chamadas levam o id dela.
3. Abas: **Departamentos**, **Agentes** (formulário de perfil, política de modelo, voz, destinos de encaminhamento, capacidades; **Validar** sem salvar; **Publicar nova versão**; lista de versões com desativar/reativar), **Skills** (pacotes de demonstração identificados como fictícios, **Revisar permissões** → instalar só depois de marcar que revisou; concessão por agente escolhendo skill, conta, operações e confirmando explicitamente), **Conexões** (somente estado e escopos; credencial só de escrita no cofre do Windows; catálogo MCP aprovado).

Nada é conectado ou contratado automaticamente: instalar uma skill não concede acesso, criar conexão não cria credencial, e as demonstrações só trazem pacotes de skill.

## API (`/api/team`)

Mesma origem apenas: o servidor exige o `Origin` exato (ou `Sec-Fetch-Site: same-origin`, que o navegador envia em GETs do mesmo site) em **toda** rota `/api/team`, inclusive GET, e `Content-Type: application/json` em POST/PUT/PATCH. Corpo até 64 KB, campos desconhecidos recusados. Sem o atendimento persistente ligado, as rotas respondem 409 (`persistence_disabled`).

Rotas por empresa (`/api/team/companies/{id}/…`): departamentos, `agents` (+ `validate`, `versions`), `skills` (+ `demos`, `review`, `install`), `grants`, `connections` (+ `credential`), `connectors` (catálogo de conectores; ver [TEAM-CONNECTORS.md](TEAM-CONNECTORS.md)), `mcp-catalog`. Empresa inexistente, id de outra empresa ou recurso invisível respondem **o mesmo 404** ("Recurso nao encontrado."); listas só devolvem o que a empresa possui. Conflito de revisão → 409; dados inválidos → 400 com caminhos de campo, nunca valores; falhas inesperadas → 500 genérico sem detalhes.

**Segredos:** `PUT …/credential` recebe o valor uma vez; nenhuma resposta (nem erro) o devolve, só estado (`ativa`, `revogada`, onde está guardada). O campo é `type=password`, sem autocompletar, e é limpo depois de cada tentativa.

## Microfone e conversa

A seção Equipe não usa o microfone. Ao sair da Conversa para qualquer outra seção, a captura e a reprodução são desligadas (`stopAll`), para que a captura nunca continue fora de vista; voltar não a religa: só o botão explícito do chat.

## Verificação

- API: `tests/test_team_web.py` (12): origem/JSON, persistência desligada, entradas inválidas, fluxo de empresa/departamento/agente/versões, validação sem salvar, isolamento entre empresas, skills (prévia, hash revisado, manifestos inválidos), concessões e revogação, credenciais somente de escrita (nenhuma resposta contém o segredo).
- Interface: `frontend/tests/team.test.js` (helpers de formulário, mensagens, segredos, cliente da API); `npm run build` e `npm test`.
- Visual: conferido no navegador em ~767 px de largura (Equipe, empresa selecionada, abas, pacotes de demonstração identificados); ativação do atendimento persistente pela própria tela. Durante a conferência apareceu um defeito real, corrigido: o navegador não envia `Origin` em GET do mesmo site e o guard recusava; agora `Sec-Fetch-Site: same-origin` vale como prova (o menu "Atendimento persistente" do chat sofria do mesmo problema).

## Limites

Não foi possível reduzir a janela para largura de celular (< 680 px) na conferência visual, então esse layout e o Electron não foram verificados visualmente (o CSS tem pontos de quebra, mas sem captura). Não há edição de política de modelo nem de capacidades na tela além do campo de texto; o catálogo MCP aceita só servidores HTTP e a classificação de ferramentas (`tool_specs`) não é editável aqui. Sem painel de demandas, aprovações, filas ou mensagens (T16). Sem autenticação além do operador local (T32).
