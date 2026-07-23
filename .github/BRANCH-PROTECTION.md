# Proteção da branch `main`

> **Status: PENDENTE — bloqueado pelo plano do GitHub.**
> Em repositório privado no plano free, a API de branch protection e a de
> rulesets retornam `403 "Upgrade to GitHub Pro or make this repository
> public"` (mesmo caso do mcp_brain, issue #14 de lá). A configuração abaixo
> está pronta; falta só o pré-requisito de conta.

## Objetivo

- `main` protegida exigindo 3 status checks verdes antes do merge:
  `Lint (ruff)`, `Varredura de segredos (gitleaks)`, `Testes (unit)`.
- Push direto na `main` bloqueado (mudanças só via PR).
- Ao menos 1 review aprovando o PR.

> Os contexts usam o **nome dos jobs** do [ci.yml](workflows/ci.yml), que é o que
> aparece como check run no PR — não os ids `lint`/`secrets`/`test`.

## Como habilitar (escolha UMA das opções de pré-requisito)

1. **Tornar o repositório público**, ou
2. **Assinar GitHub Pro/Team** (mantém privado).

## Aplicar via CLI (ruleset — recomendado)

O JSON já está versionado em [rulesets/protect-main.json](rulesets/protect-main.json):

```bash
gh api -X POST repos/CidLucas/ops-centro/rulesets \
  --input .github/rulesets/protect-main.json
```

Para atualizar depois: `gh api -X PUT repos/CidLucas/ops-centro/rulesets/<id> --input .github/rulesets/protect-main.json`.

## Aplicar via UI

Settings → Rules → Rulesets → **New ruleset** → **Import a ruleset** e selecione
`.github/rulesets/protect-main.json`. Deixe o enforcement em **Active**.

## Verificar

```bash
gh api repos/CidLucas/ops-centro/rulesets            # lista rulesets ativos
gh api repos/CidLucas/ops-centro/branches/main --jq .protected
```
