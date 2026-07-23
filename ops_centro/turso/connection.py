"""Conexão libsql/Turso — remota (produção) ou arquivo local (dev/testes).

Padrão reaproveitado do `db/turso_client.py` do mcp_brain: nada de conexão global
implícita, o construtor aceita qualquer conexão libsql e há um caminho 100% local
sem rede para os testes. A diferença aqui é que o banco de logs (RF05) é
*append-only* e não tem tenant provisionado por cliente — uma URL só, vinda do env.

Env vars (ver docs/secrets.md):
- TURSO_DATABASE_URL  — `libsql://<db>-<org>.turso.io` em produção; um caminho de
  arquivo (`./local-logs.db`) também é aceito, para desenvolvimento offline.
- TURSO_AUTH_TOKEN    — token do database (obrigatório para URLs remotas).
"""

from __future__ import annotations

import os
from typing import Any

REMOTE_SCHEMES = ("libsql://", "https://", "wss://")


class TursoNotConfigured(RuntimeError):
    """TURSO_DATABASE_URL ausente — o chamador decide entre falhar ou virar no-op."""


def is_configured() -> bool:
    """True se há URL de banco configurada no ambiente."""
    return bool(os.environ.get("TURSO_DATABASE_URL", "").strip())


def connect(url: str | None = None, auth_token: str | None = None) -> Any:
    """Abre uma conexão libsql. `url` remota usa o driver remoto (sem replica local:
    o writer é write-only e não se beneficia de cache de leitura); qualquer outro
    valor é tratado como caminho de arquivo.

    Levanta TursoNotConfigured quando não há URL — quem quiser degradação graciosa
    checa `is_configured()` antes.
    """
    import libsql

    url = (url if url is not None else os.environ.get("TURSO_DATABASE_URL", "")).strip()
    if not url:
        raise TursoNotConfigured(
            "TURSO_DATABASE_URL não configurado (ver .env.example e docs/secrets.md)"
        )

    token = auth_token if auth_token is not None else os.environ.get("TURSO_AUTH_TOKEN", "")
    if url.startswith(REMOTE_SCHEMES):
        return libsql.connect(database=url, auth_token=token)
    return libsql.connect(database=url)
