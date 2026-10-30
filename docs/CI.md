# CI: como o `checks.yml` roda (#153)

## Quando roda

- Em todo **pull request** e em **push na `main`**. Um push numa branch com PR aberto roda **uma vez** (antes rodava duas: `push` e `pull_request`, que disputavam os runners).
- `concurrency` cancela a execução anterior da mesma ref quando chega um commit novo. Consequência: um PR atualizado mostra a execução antiga como cancelada; vale a última.

## Jobs

| Job | O que faz |
|---|---|
| `tests (1/3)`, `tests (2/3)`, `tests (3/3)` | A suíte Python (`unittest`) dividida em 3 fatias, em paralelo, no Windows |
| `configuration` | Falha se alguma fatia falhou; confere que as 3 fatias juntas são a suíte inteira, cada teste uma vez; roda os evals offline e a compilação |
| `postgres` | Testes de contrato e fila em PostgreSQL real (como antes) |
| `frontend` | `npm ci`, build e testes (como antes) |
| `desktop` | Testes do Electron e checagem de sintaxe (como antes) |

## A divisão (`scripts/test_shard.py`)

- Por **classe de teste** (a classe mantém seus fixtures juntos), balanceada pelo número de testes, de forma determinística: todo job calcula o mesmo plano.
- `python scripts/test_shard.py --shard 2 --of 3` roda a fatia 2; `--check --of 3` prova a cobertura; `--list` mostra o tamanho de cada fatia. O teste `tests/test_ci_shards.py` cobre o particionador e a suíte real.
- Localmente nada muda: `python -m unittest discover -s tests` continua rodando tudo.

## Números

- Antes: ~1.150 testes em série num job (18 a 62 min nos PRs de 29-30/10/2026; 21 min numa execução e 39 min na duplicada do mesmo commit).
- Por tempos medidos no log do CI, a divisão por contagem dá fatias de ~6 a ~7,5 min de testes (364, 420 e 444 s). O tempo real por PR (com a preparação de cada job) é medido no PR #153 e fica **a confirmar**.

## Limites

- Os 3 jobs Windows ocupam 3 runners ao mesmo tempo; se o limite da conta for pequeno, eles esperam na fila e o ganho diminui.
- Se uma classe nova de teste for muito pesada, uma fatia pode ficar mais lenta que as outras; o plano se reequilibra sozinho pela contagem, mas não pelo tempo.
- Não há proteção de branch na `main` hoje (a API devolveu "Branch not protected"); se for criada, o check a exigir deve ser `configuration` e as três fatias.
