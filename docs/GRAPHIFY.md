# Mapa de código para agentes

Integração com [Graphify](https://github.com/Graphify-Labs/graphify), pacote `graphifyy`, em ambiente separado `.graphify-venv`. Versões fixadas em `requirements-graphify.txt`.

```powershell
.\scripts\graphify.ps1 -Install
.\scripts\graphify.ps1 -Action explain -Query make_agent
.\scripts\graphify.ps1 -Action query -Query "LocalSTT"
```

Para atualizar após uma alteração: `.\scripts\graphify.ps1`. O comando funciona de qualquer diretório; sempre indexa a raiz deste projeto.

O wrapper usa `extract --code-only --no-cluster`: extração AST local, sem modelos, chaves ou cobrança. Gera `graphify-out/graph.json`. Não produz HTML, relatório de comunidades ou análise semântica de documentação nesta configuração mínima. Os arquivos de roadmap e contribuição devem ser lidos diretamente.

O grafo e caches são gerados localmente e não são versionados: podem conter caminhos absolutos e ficar desatualizados. `.graphifyignore` e `.gitignore` excluem modelos, ambientes, logs, configuração privada e resultados locais. As dependências de runtime não incluem Graphify.

Não executamos `graphify install` nem instalamos hooks globais. O wrapper desativa auto-refresh de skills externas via `GRAPHIFY_NO_AUTO_REFRESH=1` e restaura o valor anterior ao terminar. A integração para agentes está descrita no AGENTS.md do projeto; não depende de editar configurações globais do Codex ou Claude.

## Fluxo para agentes

1. Abrir a issue e seus critérios de aceite.
2. Gerar/atualizar o mapa, consultar símbolos e dependências com os comandos acima.
3. Confirmar as relações relevantes lendo o código: o grafo é auxiliar, não autoridade.
4. Implementar e testar a entrega; atualizar o mapa antes de analisar impacto.
5. Publicar branch/PR vinculados à issue, registrando validação e limitações.

Limitações conhecidas: scripts PowerShell sem declarações podem não gerar símbolos; arquivos CMD não são classificados. Isso não impede sua execução e revisão direta. Um resultado vazio ou símbolo ambíguo deve ser investigado no código, sem presumir ausência de dependência.
