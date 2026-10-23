# Execução das issues por Codex ou Claude

Este guia é neutro em relação ao agente de desenvolvimento. As skills do produto planejado não são as skills internas do Codex/Claude. Trabalhar somente na issue selecionada e entregar uma parte verificável por PR.

## Fontes de verdade

1. Instruções do usuário, `AGENTS.md` e `CONTRIBUTING.md`.
2. Critérios da issue selecionada e decisões explicitamente registradas nela.
3. [Arquitetura](TEAM-ARCHITECTURE.md), [backlog legível](TEAM-BACKLOG.md) e [manifesto de dependências](team-backlog.json).
4. Código atual e testes; Graphify auxilia a localizar relações, mas pode estar desatualizado.

O manifesto é o plano versionado, não um espelho de status do GitHub. Consultar estado e PRs das dependências antes de começar. Número da issue não substitui dependência: a menor elegível é uma ordem válida; não precisa implementar todas as anteriores se não forem dependências. O acompanhamento #15 não é uma issue de implementação e não deve ser fechado por uma filha.

## Escolher a próxima entrega

- Ler a issue por inteiro, confirmar seu estado e os bloqueios. Começar por **#16 / T01** quando nenhuma entrega da equipe estiver implementada.
- Uma dependência precisa estar implementada e validada na `main`. Se foi fechada como descartada ou substituída, conferir a decisão; não assumir que está pronta.
- Checar PR ou branch em andamento para evitar trabalho duplicado. Cada executor mantém uma única issue ativa por vez, salvo instrução humana explícita de colaboração.
- Se faltar uma decisão necessária, registrar a pergunta na conversa com o operador e no plano quando autorizado; não inventar provedor, cliente, orçamento ou permissão. Pode selecionar outra issue independente elegível, sem declarar a anterior entregue.
- T26 e T27 dependem da escolha de registrador/ERP; T19/T20 exigem configuração externa para smoke real; T32 exige destino/identidade antes de exposição remota. O runtime local e testes fake podem avançar onde o aceite permitir.
- As 37 entregas são o backlog conhecido. Se uma revelar complexidade maior, subdividir **antes** de implementar uma entrega excessiva, atualizando dependências e acompanhamento. Não misturar duas issues no mesmo PR.

## Iniciar uma issue

```powershell
git status --short
gh issue view NUMERO
gh pr list --state open
```

Preservar alterações do usuário. Atualizar a base quando o checkout estiver seguro e criar branch `feat/NUMERO-descricao` ou `fix/NUMERO-descricao`, conforme CONTRIBUTING.md. Não fazer reset/limpeza destrutiva para obter estado limpo. Se já houver uma branch/PR adequado, continuar nele.

Consultar o mapa pelo wrapper, por exemplo:

```powershell
.\scripts\graphify.ps1 -Action explain -Query make_agent
```

Confirmar no código os símbolos encontrados. Registrar na descrição do PR o escopo efetivamente entregue e as limitações. Se mudar contrato, atualizar schema_version/migração e testes de compatibilidade previstos na issue. Evitar refatorações ou migrações de framework sem relação com o aceite.

## Verificar a entrega

Cada issue define sua validação específica. Não criar testes que apenas repitam implementação; priorizar invariantes e falhas relevantes: escopo de empresa, aprovação exata, dono único, duplicata e recuperação de crash quando aplicáveis.

Na base atual, os comandos de verificação disponíveis são:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
npm test --prefix frontend
npm run build --prefix frontend
node --test desktop/tests/*.test.cjs
git diff --check
```

Rodar os verificadores pertinentes aos arquivos alterados e os gates exigidos pela CI. Documentação isolada não exige executar modelo ou consumir API. Mudanças de comportamento exigem testes adequados. Novos conectores devem ter doubles/fixtures sintéticos; smoke real é separado, opt-in e usa contas/destinatários autorizados. Não alegar validação real só porque um contrato fake passou.

Atualizar o Graphify quando houver código alterado. Incluir novos módulos/schemas/migrações no empacotamento quando necessário. Verificar a allowlist do portable e as exclusões de `.gitignore`/`.graphifyignore` ao criar banco, anexos ou caches. Nunca colocar dados de uma empresa real em fixture, comentário, issue, relatório, build ou grafo publicado.

## Publicar e concluir

1. Revisar diff e arquivos adicionados. Fazer commit, push e PR com referência à issue.
2. Explicar problema, comportamento final, testes e limites. `Closes #NUMERO` somente se todo o aceite estiver cumprido; não fechar #15 junto.
3. Conferir CI e review. Merge exige autorização vigente do usuário; não inferir autorização de compra/envio por haver autorização de merge.
4. Confirmar código na `main`, estado da issue e checklist do acompanhamento. Atualizar manifesto se houve mudança de dependências, sem guardar status que envelhece a cada execução.
5. Informar resultado e próxima issue elegível. Continuar desenvolvimento somente se estiver dentro da instrução atual do usuário; preparar o backlog não autoriza executar imediatamente todas as issues.

## Prompt reutilizável

> Leia AGENTS.md, CONTRIBUTING.md, docs/TEAM-ARCHITECTURE.md, docs/TEAM-BACKLOG.md e docs/TEAM-IMPLEMENTATION.md. Consulte o manifesto docs/team-backlog.json e o estado atual no GitHub. Selecione a próxima issue de implementação elegível, confirme o aceite e as dependências entregues na main. Trabalhe apenas nela, preservando execução local gratuita, isolamento por empresa, credenciais fora de prompts/arquivos públicos e compatibilidade existente. Consulte o Graphify e confirme no código. Entregue uma branch e um PR, execute verificações pertinentes, documente limitações e não declare teste real que não executou. Não faça ações externas de negócio ou merge sem autorização aplicável. Se faltar decisão essencial de provedor, não invente: identifique a pendência e trabalhe numa issue independente elegível somente quando autorizado.

## Critérios de conclusão do programa

Equipe completa significa as entregas do acompanhamento implementadas, validadas e documentadas, ou retiradas do escopo por decisão explícita do operador. Um cenário demonstrativo não torna provedores reais concluídos. Integrações bloqueadas por fornecedor/credenciais continuam abertas com dependência explicada. Não mover status para concluído para produzir um relatório sem pendências.
