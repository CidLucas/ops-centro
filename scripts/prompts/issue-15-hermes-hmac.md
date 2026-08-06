# Ajuste no receiver: assinar o POST ao webhook do Hermes com HMAC-SHA256

## Contexto

Repo: `/tmp/ops-centro` (CidLucas/ops-centro). A rota `ops-centro-alerts` do Hermes
gateway (webhook platform na porta 8644) **exige HMAC-SHA256** em todo POST
(`X-Hub-Signature-256: sha256=<hex>` — formato GitHub, verificado no adapter
`gateway/platforms/webhook.py` do Hermes: `hmac.new(secret, raw_body, sha256).hexdigest()`).
Hoje `ops_centro/receiver/hermes.py` manda só `X-Hermes-Token`/`Authorization` (token
simples) → o gateway rejeitaria com 401. O secret é o valor de `HERMES_WEBHOOK_TOKEN`
(que agora é o secret da assinatura, não mais um token livre).

## Tarefa

Edite **apenas** `ops_centro/receiver/hermes.py` e `tests/test_hermes.py`.

### 1. Imports (junto dos existentes, ~linha 38)

```python
import hashlib
import hmac
```

### 2. `_auth_headers()` (linha ~320) — assinar o corpo quando disponível

```python
def _auth_headers(corpo: bytes | None = None) -> dict[str, str]:
    """Token nos dois formatos + assinatura HMAC-SHA256 do corpo (X-Hub-Signature-256)."""
    token = os.environ.get("HERMES_WEBHOOK_TOKEN", "").strip()
    if not token:
        return {}
    headers = {"X-Hermes-Token": token, "Authorization": f"Bearer {token}"}
    if corpo is not None:
        assinatura = hmac.new(token.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={assinatura}"
    return headers
```

### 3. No envio (linha ~497) — serializar UMA vez e assinar os MESMOS bytes

Hoje:
```python
    payload = notification.as_payload()
    headers = _auth_headers()
    ...
                resposta = await http.post(destino, json=payload, headers=headers, timeout=limite)
```

Vira:
```python
    payload = notification.as_payload()
    corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _auth_headers(corpo)
    ...
                resposta = await http.post(
                    destino,
                    content=corpo,
                    headers={**headers, "Content-Type": "application/json"},
                    timeout=limite,
                )
```

Regra de ouro: a assinatura é calculada sobre os bytes EXATOS enviados (serializa → assina →
envia o mesmo objeto). Nada de `json=` do httpx depois de assinar.

### 4. Testes (`tests/test_hermes.py`)

- No teste de entrega com mock (ex: `test_entrega_no_primeiro_ok` e o helper de mock do
  client), capture o `content` enviado e afirme:
  1. `content == json.dumps(payload, ensure_ascii=False).encode("utf-8")`
  2. header `X-Hub-Signature-256 == "sha256=" + hmac.new(token, content, hashlib.sha256).hexdigest()`
  3. `X-Hermes-Token` e `Authorization` continuam presentes (token do env de teste)
- Novo teste: `HERMES_WEBHOOK_TOKEN` vazio → `_auth_headers()` retorna `{}` e o POST sai
  sem headers de auth (comportamento atual preservado).
- Se o harness de teste monta `payload` via `notification.as_payload()`, use a MESMA
  chamada no assert do content (não recrie o dict na mão).

## Critérios de aceite

1. `uv run python -m pytest tests/test_hermes.py -q` verde; depois `uv run python -m pytest tests/ -q` completo verde.
2. `uv run ruff check ops_centro tests` limpo.
3. Nenhuma mudança fora de `hermes.py` e `test_hermes.py`.
4. NÃO commitar — deixe o working tree pronto para revisão. Reporte o diff resumido.
