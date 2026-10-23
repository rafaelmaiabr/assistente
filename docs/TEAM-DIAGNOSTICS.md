# Diagnóstico de HTTP, DNS e domínio (T23)

Entrega da issue [#38](https://github.com/rafaelmaiabr/assistente/issues/38): `team/diagnostics.py` (verificações e conclusão), `team/diagnostics_mcp.py` (ativos autorizados, servidor MCP local), migração `0014`, `scripts/smoke_diagnostics.py`. Usa conexões/catálogo ([TEAM-CONNECTIONS.md](TEAM-CONNECTIONS.md)), skills ([TEAM-SKILLS.md](TEAM-SKILLS.md)) e o gateway ([TEAM-GATEWAY.md](TEAM-GATEWAY.md)). Biblioteca padrão apenas.

Estado: implementado e testado, mas **não ligado ao `TeamApp` nem a uma tela** (o gateway real ainda não está ligado ao app) e sem skill de demonstração empacotada. É somente leitura: não altera DNS, domínio ou hospedagem, não renova nem paga nada.

## Ativos autorizados

Contrato novo `monitored_asset` (nome DNS minúsculo, por empresa, único). Só o **operador** cadastra, ativa e desativa (`AssetService`); agentes não adicionam alvos. Um host é permitido se for um ativo ativo ou subdomínio dele. Qualquer outra coisa (outro domínio, IP, `localhost`, URL, porta no nome, nome de outra empresa) é recusada com `nao_autorizado` **antes de qualquer consulta de rede**.

## Ferramentas (todas `read`, argumento único `dominio`)

| Ferramenta | O que faz |
|---|---|
| `verificar-dns` | resolve o nome (resolvedor do sistema) e valida os endereços |
| `verificar-http` | `GET /` por HTTPS, seguindo redirecionamentos só entre hosts autorizados |
| `verificar-tls` | certificado: validade, dias restantes, emissor, aviso de vencimento próximo (14 dias) |
| `consultar-registro` | RDAP (via `rdap.org`): data de expiração, status, registrador; tenta o nome e seus pais (máx. 4) |
| `diagnosticar-site` | executa as verificações, consulta o registro só se algo estiver errado e devolve a conclusão |

Cada evidência traz **fonte**, **timestamp** e **certeza** (`confirmado`, `provavel`, `incerto`). A conclusão traz `categoria`, `certeza`, `resumo` e `proximo_passo`, sempre com `somente_leitura: true`.

## Não presumir domínio vencido

| Situação | Categoria | Certeza |
|---|---|---|
| RDAP com expiração passada ou status de resgate/exclusão | `dominio_expirado` | confirmado |
| RDAP com status de retenção (hold) | `dominio_retido` | confirmado |
| Nome não resolve, registro vigente | `dns_sem_resposta_dominio_vigente` (zona/nameservers, não expiração) | provável |
| Nome não resolve, RDAP indisponível ou sem dados | `suspeita_de_expiracao` ("hipótese, não fato"; confirmar no registrador) | incerto |
| Nome não resolve, RDAP sem registro | `dominio_nao_encontrado` | provável |
| Falha temporária do resolvedor | `dns_instavel` | incerto |
| Certificado vencido / nome divergente / não confiável | `certificado_*` | confirmado |
| Sem conexão ou erro 5xx / 4xx | `servidor_inacessivel`, `erro_do_servidor`, `resposta_de_erro` | confirmado/provável |
| Tudo normal | `site_ok` | confirmado |

RDAP sem data de expiração não vira "vencido" (`sem_data_de_expiracao: true`). O resolvedor do sistema não distingue NXDOMAIN de resposta sem dados; por isso a falta de resolução nunca é tratada como prova de expiração.

## Defesa SSRF

- Resolução: **todos** os endereços devem ser globalmente roteáveis. Bloqueados: privados, loopback, link-local (inclui `169.254.169.254` de metadados de nuvem), CGNAT (`100.64/10`), multicast, reservados, `0.0.0.0`, IPv6 local/ULA, 6to4/Teredo e IPv4 embutido em IPv6 (verificado como IPv4). Uma resposta mista (pública + privada) também é bloqueada.
- **Anti-rebinding**: a conexão usa o endereço já validado (uma resolução por requisição, sem nova consulta antes de conectar).
- Redirecionamentos: máx. 3; destino IP, esquema diferente de http/https, porta diferente de 80/443 ou host fora dos ativos **não é seguido** e aparece como observação. O RDAP segue só redirecionamentos HTTPS para hosts públicos.
- Sem varredura: só portas 80/443, só ativos cadastrados, no máximo 8 requisições por diagnóstico, tempo de 8 s por requisição e 256 KiB por resposta.

## Catálogo

`approve_catalog` registra `diagnostico-rede` com o transporte `local` ([TEAM-IMAP.md](TEAM-IMAP.md) descreve o transporte). `DiagnosticsTransport` roda as verificações bloqueantes em thread e devolve `nao_autorizado` como resultado estruturado.

## Validação

`tests/test_team_diagnostics.py` (22 testes) com rede falsa: domínio expirado (RDAP), RDAP sem data, NXDOMAIN com domínio vigente, TLS vencido/divergente/não confiável, certificado perto do vencimento, erro 5xx, timeout, redirecionamento interno/IP/esquema/porta/host não autorizado e laços, DNS rebinding (resposta mista e troca de endereço), tabela de endereços bloqueados, nomes inválidos, orçamento de requisições, ferramentas pelo gateway, isolamento entre empresas e argumentos extras.

**Rede pública real** (manual, `python scripts/smoke_diagnostics.py <dominio>`), executada em 2026-10-23 contra `example.com` (site ok, RDAP com expiração e registrador), um domínio inexistente (`dominio_nao_encontrado`, provável) e `expired.badssl.com` (`certificado_vencido`, confirmado). Não foi testado com um domínio realmente expirado nem com TLDs sem RDAP (`.br` usa RDAP do Registro.br via bootstrap; não verificado aqui).

## Limites

Sem consultas DNS por tipo (MX, TXT, NS, CNAME): a biblioteca padrão só oferece o resolvedor do sistema, então não há registros além de A/AAAA. Dependência do serviço público `rdap.org` (pode limitar taxa ou falhar; vira `indisponivel`/`incerto`). Sem checagem de propagação por vários resolvedores, de e-mail (MX/SPF) nem de conteúdo da página. Não há tela, agendamento nem alerta de vencimento (T24+).
