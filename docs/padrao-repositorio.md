# Padrão de repositório

Este documento descreve o padrão que os repositórios devem seguir, extraído do
que já está em produção no **ops-centro** (que por sua vez herdou do `mcp_brain`).
Serve para dois usos: abrir repositório novo sem redecidir nada, e auditar
repositório existente contra uma referência.

O princípio que organiza tudo abaixo: **o que o CI cobra, o `make` reproduz na
máquina do dev, com o mesmo comando**. Divergência entre os dois é bug de
processo — o dev descobre a quebra no PR em vez de descobrir antes de abrir.

- [1. Estrutura de pastas](#1-estrutura-de-pastas)
- [2. Arquivos mínimos](#2-arquivos-mínimos)
- [3. Gerenciamento de dependências](#3-gerenciamento-de-dependências)
- [4. Makefile](#4-makefile)
- [5. Docker](#5-docker)
- [6. CI](#6-ci)
- [7. CD](#7-cd)
- [8. Jobs agendados](#8-jobs-agendados)
- [9. Segredos e configuração](#9-segredos-e-configuração)
- [10. Banco e migrations](#10-banco-e-migrations)
- [11. Testes](#11-testes)
- [12. Documentação](#12-documentação)
- [13. Governança do repositório](#13-governança-do-repositório)
- [14. Convenções de código](#14-convenções-de-código)
- [15. Checklist de repositório novo](#15-checklist-de-repositório-novo)

---

## 1. Estrutura de pastas

```
<repo>/
├── .github/
│   ├── workflows/           ci.yml, cd.yml + jobs agendados (um arquivo por job)
│   ├── rulesets/            protect-main.json — proteção da main versionada
│   ├── dependabot.yml
│   └── BRANCH-PROTECTION.md como aplicar/verificar o ruleset
├── <pacote>/                código da aplicação — nome_com_underscore
│   ├── __init__.py
│   ├── <subdomínio>/        um subpacote por área (ex: receiver/, turso/, grafana/)
│   └── *.py                 módulos com `__main__` quando forem entrypoint de make
├── tests/                   test_<módulo>.py espelhando o pacote; sem subpastas
├── db/migrations/           NNNN_nome.sql numeradas, imutáveis depois de aplicadas
├── deploy/                  tudo que só existe no servidor: compose de prod,
│                            script de deploy, proxy, coleta de segredos
├── docs/                    um .md por área funcional, referenciados pelo README
├── <artefatos-as-code>/     saída gerada por código e versionada (ex: grafana/)
├── .env.example             toda variável, comentada, sem nenhum valor real
├── .gitignore
├── .gitleaks.toml
├── Dockerfile
├── docker-compose.yml       stack de DESENVOLVIMENTO (build: .)
├── Makefile
├── pyproject.toml
├── uv.lock                  versionado, sempre
└── README.md
```

Regras que a árvore encodifica:

- **`deploy/` é separado da raiz.** O `docker-compose.yml` da raiz builda local; o
  `deploy/docker-compose.yml` consome imagem do registry. São arquivos diferentes
  porque respondem a perguntas diferentes; unificá-los com `override` esconde de
  quem deploia qual é o comportamento real em produção.
- **Artefato gerado por código fica versionado.** Se um módulo gera JSON/YAML de
  configuração (dashboards, alertas, políticas), o gerador vive no pacote e a
  saída vive numa pasta de topo. O diff da saída no PR é o que torna a mudança
  revisável — ver §6 (gate `--check`).
- **`tests/` é plano.** Um `test_<módulo>.py` por módulo. Espelhar a hierarquia do
  pacote em subpastas só adiciona `__init__.py` e imports frágeis.

---

## 2. Arquivos mínimos

Repositório sem estes arquivos não está pronto para receber o segundo commit:

| Arquivo | Obrigatório porque |
| --- | --- |
| `README.md` | Índice navegável: o que vive aqui, com link por área, e os comandos de desenvolvimento. Ver §12. |
| `Makefile` | Superfície única de comandos. `make help` é a documentação executável. |
| `pyproject.toml` | Deps, build, ruff, pytest e coverage num arquivo só. Sem `setup.py`, `requirements.txt` ou `.flake8` soltos. |
| `uv.lock` | Build reproduzível. `--frozen` no CI e no Docker cobra que ele esteja em dia. |
| `Dockerfile` | Multi-stage, non-root. Ver §5. |
| `docker-compose.yml` | Stack local com `env_file: .env` e healthcheck. |
| `.env.example` | Contrato de configuração. Toda variável documentada, nenhum valor. |
| `.gitignore` | Mínimo: `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.coverage`, `.ruff_cache/`, `.DS_Store`. |
| `.gitleaks.toml` | Estende as regras default; allowlist vazia, preenchida só quando aparecer falso-positivo real. |
| `.github/workflows/ci.yml` | Gate de qualidade. Ver §6. |
| `.github/workflows/cd.yml` | Build + smoke + publicação. Ver §7. |
| `.github/dependabot.yml` | Ecossistemas `uv` e `github-actions`, semanal, agrupados. |
| `.github/rulesets/protect-main.json` | Proteção da `main` como código, não como clique na UI. |
| `.github/BRANCH-PROTECTION.md` | Como aplicar e verificar o ruleset, e o que bloqueia se ainda não estiver ativo. |

Serviço com deploy próprio acrescenta: `deploy/docker-compose.yml`, `deploy/deploy.sh`
e o script de coleta de segredos (§9). Serviço com estado acrescenta `db/migrations/`.

---

## 3. Gerenciamento de dependências

**uv, sempre.** Nunca pip/poetry/pipenv no mesmo repo.

```toml
[project]
requires-python = ">=3.12,<3.14"   # piso e teto explícitos
dependencies = [ ... ]             # sem pin exato; o lock é quem pina

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "ruff"]
```

Regras:

1. **`uv.lock` versionado e instalado com `--frozen`** — no CI, no Dockerfile e no
   `make install`. Sem `--frozen`, o uv resolve silenciosamente e o build deixa de
   ser reproduzível.
2. **Dev deps em `[project.optional-dependencies] dev`**, instaladas com
   `--extra dev`. A imagem de produção usa `--no-dev`.
3. **Dependência interna vem de git pinada por commit**, com comentário dizendo o
   que aquele rev contém:

   ```toml
   [tool.uv.sources]
   minha-lib = { git = "https://github.com/org/repo.git", subdirectory = "libs/minha_lib", rev = "<sha completo>" }
   ```

   Nunca `branch = "main"`. Bump de rev é um commit deliberado, revisável, que
   dispara o CI. Se a lib vem de git, o **builder do Docker precisa de `git`** —
   ver §5.
4. **Toda dependência usada diretamente é declarada**, mesmo que já chegue
   transitivamente. Transitiva desaparece sem aviso quando o pai troca.
5. **Dependabot semanal**, com `groups` para minor/patch num PR só, e labels
   (`dependencies`, `ci`) para o PR chegar já triado.

---

## 4. Makefile

Contrato: `.DEFAULT_GOAL := help`, todo alvo com `## descrição`, alvos agrupados por
seção com uma linha de comentário-régua, e `help` gerado por `grep`+`awk` a partir
dos próprios `##`.

```make
.DEFAULT_GOAL := help

.PHONY: help
help: ## Lista os alvos disponíveis
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
```

**Alvos obrigatórios**, com estes nomes exatos:

| Alvo | Faz |
| --- | --- |
| `install` | `uv sync --frozen --extra dev` |
| `env` | Cria `.env` a partir do `.env.example` **sem sobrescrever** |
| `lint` | `uv run ruff check .` — mesmo comando do CI |
| `test` | `uv run pytest -m unit` — mesmo comando do CI |
| `cov` | Testes com `--cov-report=term-missing` |
| `run` | Sobe o serviço local |
| `up` / `down` / `logs` / `ps` / `build` | `docker compose` via `$(COMPOSE)` |
| `clean` | Remove caches de build/pytest e `__pycache__` |

Alvos de domínio (migrations, jobs, publicação de artefato as-code) seguem a mesma
forma: **um alvo por entrypoint `python -m`**, e a descrição cita a doc ou a issue
correspondente. O alvo é a interface; ninguém precisa lembrar o nome do módulo.

Alvo cujo comando aparece num workflow deve ser **literalmente o mesmo comando**.
Se o CI roda `pytest -m unit --cov-fail-under=80`, o `make test` roda `pytest -m unit`
e o `make cov` mostra o que falta — não uma variação que passa localmente e quebra no PR.

---

## 5. Docker

**Dockerfile multi-stage, runtime non-root:**

```dockerfile
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/     # versão do uv pinada
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*                         # só se houver dep git
WORKDIR /app
COPY pyproject.toml uv.lock ./                             # camada de deps separada
RUN uv sync --frozen --no-dev --no-install-project
COPY <pacote>/ <pacote>/
COPY README.md ./                                          # se o pyproject o declara em `readme`
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8080
CMD [...]
```

Pontos não-negociáveis:

- **Deps antes do código.** Trocar uma linha de Python não pode invalidar a camada
  de `uv sync`.
- **`USER` non-root**, e o CD verifica (`id -u != 0`). Container que roda como root
  não chega ao registry.
- **`--no-dev` no runtime.** Pytest e ruff não vão para produção.
- **Versão do uv pinada** no `COPY --from`. `:latest` faz o build de hoje diferir
  do de ontem sem nenhum commit.

**Compose local** (`docker-compose.yml` na raiz): `build: .`, `env_file: .env`,
porta parametrizada (`${PORT:-8080}:8080`), `restart: unless-stopped` e healthcheck
batendo no mesmo `/healthz` que o CD e o deploy cobram.

**Compose de produção** (`deploy/docker-compose.yml`): `image:` do registry (nunca
`build:`), `container_name` fixo, **bind em `127.0.0.1`** com o proxy à frente,
rotação de log (`max-size`/`max-file` — disco de VM pequena enche em silêncio), e
proxy opcional atrás de `profiles: ["proxy"]` para o caso de a máquina já ter um.
Variável sem default seguro usa `${VAR:?mensagem}` para falhar no `up`, não em runtime.

---

## 6. CI

`.github/workflows/ci.yml` — roda em `push: ["**"]` e `pull_request: [main]`, com
`concurrency` cancelando runs antigas do mesmo ref e `permissions: contents: read`.

Quatro jobs, paralelos, com **nomes estáveis** (são os `context` do ruleset — mudar
o `name:` de um job quebra a proteção da `main` em silêncio):

| Job | `name:` | Comando | Bloqueia? |
| --- | --- | --- | --- |
| `lint` | `Lint (ruff)` | `uv run ruff check --output-format=github .` | sim |
| `secrets` | `Varredura de segredos (gitleaks)` | imagem `zricethezav/gitleaks` com `--redact`, `fetch-depth: 0` | sim |
| `audit` | `Audit de dependências (pip-audit)` | `uv export --frozen --no-dev` → `uvx pip-audit` | não (`continue-on-error`) |
| `test` | `Testes (unit)` | `uv run pytest -m unit --cov --cov-fail-under=<piso>` | sim |

Detalhes que importam:

- **`--output-format=github`** no ruff anota o diff do PR em vez de esconder o erro
  no log.
- **gitleaks com `fetch-depth: 0`**: varre o histórico, não só o HEAD. Segredo
  vazado num commit revertido continua vazado.
- **pip-audit não-bloqueante por padrão**, com a política escrita no próprio
  workflow: levanta o baseline de CVEs sem travar merge; promove a bloqueante
  quando o backlog estiver tratado.
- **Piso de cobertura explícito** (`--cov-fail-under`), com comentário dizendo qual
  é o piso atual e quando subir. `coverage.xml` sobe como artefato com `if: always()`.
- **Setup do Python é `astral-sh/setup-uv@v5` com `enable-cache: true`** — não
  `actions/setup-python` + pip.

**Artefato as-code tem gate de divergência.** Todo gerador que escreve arquivo
versionado expõe `--write` e `--check`; o `--check` falha se a saída divergir do
que está commitado, e roda tanto no `make <alvo>-check` quanto num teste unit. Sem
isso, alguém edita o JSON à mão e o gerador desfaz na próxima execução.

---

## 7. CD

`.github/workflows/cd.yml` — roda em `push: [main]` e tags `v*`, com
`permissions: packages: write`.

**A ordem é deliberada: build local → smoke → push.** Imagem que não passa no smoke
nunca chega ao registry, e portanto nunca chega ao servidor.

1. `docker/build-push-action` com **`push: false, load: true`** — carrega a imagem
   no daemon do runner. Cache via `type=gha, mode=max`.
2. **Tags** via `docker/metadata-action`: `latest` só na branch default,
   `sha-<commit>` sempre (é a tag que o rollback fixa), `v1.2.3` e `1.2` nas tags
   de release. O nome da imagem precisa ser minúsculo (`${GITHUB_REPOSITORY,,}`) —
   o GHCR recusa maiúscula.
3. **Smokes, nesta ordem** (do mais barato ao mais caro):
   - *entrypoints importam* — `python -c "import a, b, c"` com todos os módulos de
     topo. Pega quebra de wiring e de dependência que o build sozinho não pega;
   - *roda non-root* — `id -u` diferente de `0`;
   - *`/healthz` responde* — sobe o container, faz polling com timeout, imprime
     `docker logs` e derruba. Import que passa não garante servidor de pé.
4. **Login no GHCR e `docker push`** de cada tag (a imagem já está no daemon;
   rebuildar só para `push: true` custaria uma segunda passada).
5. **`$GITHUB_STEP_SUMMARY`** com as tags publicadas e a linha exata do comando de
   deploy. Quem for deploiar não precisa procurar na doc.

Job adicional **`validate-compose`**: copia o `.env.example` para `.env`, roda
`docker compose config -q` no compose local e no de produção (inclusive com o
profile do proxy). Erro de interpolação em YAML de produção é descoberto no PR, não
no servidor às 3h.

**O CD publica; o deploy é um passo separado e explícito** (`deploy/deploy.sh` no
servidor). Deploy automático em push é uma decisão de projeto, não o default.

### `deploy/deploy.sh`

`set -euo pipefail`, `cd "$(dirname "$0")"`, cabeçalho com as formas de uso, e
quatro garantias:

1. `.env` presente e com permissão `600`;
2. `pull` antes de `up` — registry fora do ar deixa o serviço antigo no ar;
3. `/healthz` respondendo antes de declarar sucesso — container "Up" não é serviço
   de pé;
4. em caso de falha: `docker compose logs --tail 50` e **a linha exata do rollback**
   com a tag anterior já preenchida.

Argumento posicional = tag de imagem (`./deploy.sh sha-abc123`), que é como o
rollback acontece.

---

## 8. Jobs agendados

Job recorrente (limpeza, retenção, healthcheck externo) é um workflow próprio, não
um cron no servidor: os segredos já estão no Actions, a run fica auditável sem SSH,
e não depende de a VM estar viva.

Padrão de cada um:

- `on: schedule` + **`workflow_dispatch` com inputs** (`dry_run`, etc.). Job que só
  roda no agendado não é testável.
- `concurrency` com `cancel-in-progress: false` para jobs que mutam estado —
  limpeza interrompida no meio deixa lote pela metade.
- `timeout-minutes` sempre.
- **Step "guard"**: se o segredo do serviço externo não estiver configurado, imprime
  o motivo e **sai verde**. Fork ou clone recém-criado não deve receber alerta de CI
  por não ter infra provisionada.
- **Relatório em JSON como artefato** (`--json | tee report.json`), com
  `retention-days` explícito. É o log de execução auditável do critério de aceite.
- O job também é observado: exporta a própria telemetria pelo mesmo OTLP dos apps.

Healthcheck externo (`healthcheck.yml`) merece nota: ele bate no endpoint **público,
por HTTPS**, pelo mesmo caminho que o cliente real usa, e verifica **o vencimento do
certificado** com folga (falha com < 7 dias). O healthcheck do compose reinicia
container morto, mas não vê DNS expirado, certificado vencido nem firewall fechado.
Retentativas antes de acusar (3× com espera) — alerta que grita por um pacote
perdido é alerta que se aprende a ignorar.

---

## 9. Segredos e configuração

- **Nenhum valor real no repositório.** `.env` no `.gitignore`, `.env.example`
  versionado com todas as chaves vazias e comentadas — seção por área, cada uma
  dizendo o formato esperado, o default quando vazio e a doc correspondente.
  O `.env.example` é o contrato: variável que o código lê e não está lá é bug.
- **Falha fechada.** Segredo ausente deve degradar de forma explícita e segura: o
  writer vira no-op documentado, ou o endpoint responde 503. Nunca "funciona sem
  auth quando o token não está setado".
- **Em produção, segredo vem de um cofre**, não de arquivo copiado à mão. O padrão
  aqui é AWS SSM Parameter Store com um prefixo por ambiente
  (`/<app>/<env>/<VAR>`), materializado por `deploy/env-from-ssm.sh`: o nome do
  parâmetro vira o nome da variável, `umask 077` antes de criar o arquivo, escrita
  em temp + `mv`. Rotacionar = mudar o parâmetro e rodar o script de novo.
- **Distinção secret vs. variável** no Actions: URL pública, janela de retenção e
  nome de ambiente são `vars` (aparecem no log, e é isso que torna a falha
  diagnosticável); token e connection string são `secrets`.
- **Tokens separados por capacidade.** Token de leitura, de escrita e de ingestão
  são três, não um com todos os escopos.
- **gitleaks no CI** como rede de segurança, com histórico completo.

---

## 10. Banco e migrations

- `db/migrations/NNNN_nome.sql`, numeradas sequencialmente, aplicadas em ordem
  lexicográfica. **Migration aplicada é imutável** — corrige-se com uma nova.
- Aplicador idempotente no pacote (`<pacote>/db/migrate.py` ou equivalente) com
  registro em `schema_migrations (version, applied_at, checksum)`. O **checksum
  detecta edição de migration já aplicada**, que é a falha silenciosa clássica.
- Entrypoint com `--status` (lista o aplicado) além do modo de aplicar, exposto no
  Makefile como `migrate` / `migrate-status`.
- O caminho das migrations é sobrescrevível por env var — o wheel não empacota
  `db/`, então o pacote instalado precisa poder apontar para outro lugar.
- **Todo `CREATE TABLE` com `IF NOT EXISTS`**, e todo índice justificado por uma
  consulta real do código, dita em comentário no próprio SQL.
- O comentário de cabeçalho da migration explica **por que a tabela existe** e o que
  cada coluna não-óbvia carrega, ligando à issue/requisito.

---

## 11. Testes

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "unit: testes determinísticos, sem dependências externas",
    "integration: exigem <serviços> reais",
]
```

- **Dois marcadores, e só dois.** `unit` é o gate: determinístico, sem rede, sem
  serviço externo. `integration` roda sob demanda. Todo arquivo declara
  `pytestmark = pytest.mark.unit` no topo.
- **Um `test_<módulo>.py` por módulo**, com nomes de teste em português descrevendo
  o comportamento e citando o requisito
  (`test_atributos_obrigatorios_do_rf02`), não o método testado.
- **`monkeypatch` para env e relógio**; nada de `time.sleep` nem chamada real.
- **Gerador de artefato as-code tem teste de divergência** que roda o mesmo
  `--check` do Makefile.
- Piso de cobertura no CI, subindo conforme o serviço ganha lógica. `[tool.coverage.run]`
  com `source = ["<pacote>"]`.

---

## 12. Documentação

**README** é índice, não manual. Nesta ordem:

1. Uma frase do que o serviço é, e para quem;
2. Diagrama ASCII da arquitetura — sete linhas, não trinta;
3. **Tabela "O que vive neste repo"**: uma linha por área, com link para o
   código *e* para a doc daquela área. É o mapa que substitui o tour guiado;
4. Blocos de `make` por fase do projeto, comentados;
5. Seção **CI/CD** listando cada workflow com um resumo do que ele cobra.

**`docs/`**: um arquivo por área funcional, nomeado pelo domínio
(`alertas.md`, `deploy.md`, `secrets.md`), sempre linkado da tabela do README. Doc
que ninguém alcança pelo README não existe.

Cada doc explica **por que aquilo é assim**, não só como usar: a decisão, o que foi
descartado, e a armadilha que a motivou. Comando na doc é copiável e verdadeiro.

---

## 13. Governança do repositório

- **`main` protegida por ruleset versionado** em `.github/rulesets/protect-main.json`:
  regras `deletion`, `non_fast_forward`, `pull_request` (≥ 1 review,
  `dismiss_stale_reviews_on_push`) e `required_status_checks` com
  `strict_required_status_checks_policy: true`. Os `context` são os **nomes dos
  jobs** do `ci.yml`, não os ids.
- Aplicado por CLI (`gh api -X POST repos/<org>/<repo>/rulesets --input ...`), com o
  procedimento e a verificação em `.github/BRANCH-PROTECTION.md`. Se algum
  pré-requisito bloqueia (plano do GitHub, por exemplo), o arquivo registra o
  bloqueio em vez de deixar a proteção como pendência invisível.
- **Branch por feature/fase**, PR para `main`, merge só com os checks verdes.
- **Commits e PRs referenciam issues** (`(#15, #16, #17)`). Mensagem de commit em
  português, no imperativo, descrevendo o efeito — não o arquivo tocado.

---

## 14. Convenções de código

- **Ruff é o único linter/formatter.** Config no `pyproject.toml`:
  `line-length = 100`, `target-version` casando com o `requires-python`, e
  `select = ["E4", "E7", "E9", "F", "I"]` — bugs reais (import não usado, nome
  indefinido) e ordem de import, não estilo cosmético.
  `per-file-ignores` para `tests/**` com `["E402", "F401", "F841"]`.
- `from __future__ import annotations` no topo dos módulos.
- **Todo módulo tem docstring** que diz o que ele resolve, o requisito/issue de
  origem e as formas de uso do `python -m` quando for entrypoint.
- **Módulo executável expõe `argparse`** com flags que espelham os alvos do
  Makefile, e `--json` quando a saída for consumida por outro processo.
- **Comentário explica o porquê, nunca o quê.** O padrão do repo é comentário de
  bloco antes da decisão não-óbvia, citando a armadilha concreta que ele evita
  ("sem volume, cada restart pede certificado novo e bate no rate limit"). Isso vale
  igualmente para YAML de workflow, SQL, Dockerfile e shell — os comentários mais
  valiosos deste repo não estão no Python.
- Idioma: **código e identificadores em inglês; docstrings, comentários, docs e
  mensagens ao usuário em português.**

---

## 15. Checklist de repositório novo

```
[ ] pyproject.toml     — deps, [tool.uv.sources] pinado por sha, ruff, pytest, coverage
[ ] uv sync            — uv.lock gerado e commitado
[ ] .gitignore + .gitleaks.toml
[ ] .env.example       — toda variável, comentada, vazia
[ ] Makefile           — help, install, env, lint, test, cov, run, up/down/logs/ps/build, clean
[ ] Dockerfile         — multi-stage, uv pinado, --no-dev, USER non-root
[ ] docker-compose.yml — build local, env_file, healthcheck
[ ] ci.yml             — lint | secrets | audit | test, nomes de job estáveis
[ ] cd.yml             — build local → 3 smokes → push GHCR + validate-compose
[ ] dependabot.yml     — uv + github-actions, semanal, agrupado
[ ] rulesets/protect-main.json + BRANCH-PROTECTION.md
[ ] README.md          — tabela de áreas + blocos de make + seção CI/CD
[ ] docs/              — um .md por área, todos linkados no README
[ ] tests/             — marker unit, piso de cobertura no CI
[ ] deploy/            — compose de prod, deploy.sh, coleta de segredos   (se tem deploy)
[ ] db/migrations/     — aplicador com checksum, make migrate/migrate-status  (se tem estado)
```

Um repositório está "no padrão" quando um dev novo consegue, sem perguntar nada:
`make env && make install && make test`, e um deploy consegue ser desfeito com uma
linha que o próprio script de deploy imprimiu.
