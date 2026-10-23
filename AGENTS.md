# Instruções para agentes

- Antes de qualquer implementação, criar ou selecionar uma issue existente e ler seu aceite. Nenhuma implementação sem issue.
- Seguir CONTRIBUTING.md e docs/ROADMAP.md. Trabalhar em partes entregáveis, uma branch e PR por issue.
- Preservar execução local sem APIs pagas por padrão. Não publicar config.json, logs, conversas, áudio, modelos ou credenciais.
- Consultar o mapa Graphify quando disponível; confirmar conclusões no código. Grafo pode estar desatualizado.
- Gerar o mapa com `scripts/graphify.ps1`; consultar `docs/GRAPHIFY.md`. Usar o wrapper para evitar auto-refresh de skills globais. Instalação opcional: `scripts/graphify.ps1 -Install`.
- Atualizar documentação e executar verificações relevantes antes do push. Relatar limitações sem alegar testes não realizados.
- Não fazer merge automático sem autorização do usuário. Não confundir demonstração MCP com acesso real a email.
- Para evolução da equipe de agentes, consultar docs/TEAM-ARCHITECTURE.md, docs/TEAM-BACKLOG.md, docs/team-backlog.json e docs/TEAM-IMPLEMENTATION.md. Selecionar uma única issue elegível, confirmar dependências entregues na main e não fechar o acompanhamento #15 em PRs das filhas. Planejamento não equivale a implementação pronta nem autoriza ações externas de negócio.
