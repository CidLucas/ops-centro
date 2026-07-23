"""Aplicador idempotente das migrations de `db/migrations/` (RF05).

Mesma abordagem do `db/migrate.py` do mcp_brain (registro em `schema_migrations`
com checksum para detectar edição de migration já aplicada), reduzida ao que este
repo precisa: sem placeholders de render e sem multi-tenant.

Uso:
    uv run python -m ops_centro.turso.migrate            # aplica no banco do env
    uv run python -m ops_centro.turso.migrate --status   # só lista o que já foi aplicado
    uv run python -m ops_centro.turso.migrate --database ./local-logs.db
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from ops_centro.turso.connection import TursoNotConfigured, connect

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def migrations_dir() -> Path:
    """Diretório das migrations. `OPS_CENTRO_MIGRATIONS_DIR` sobrescreve — necessário
    quando o pacote roda instalado (wheel não empacota `db/`)."""
    override = os.environ.get("OPS_CENTRO_MIGRATIONS_DIR")
    return Path(override) if override else DEFAULT_MIGRATIONS_DIR


def _checksum(sql_text: str) -> str:
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def _split_statements(sql: str) -> list[str]:
    """Separa por ';' depois de remover comentários (que podem conter ';')."""
    lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        if line.strip():
            lines.append(line)
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def apply_migrations(conn: Any, directory: Path | None = None) -> list[str]:
    """Aplica as migrations pendentes na conexão dada. Retorna as aplicadas agora."""
    directory = directory or migrations_dir()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  checksum TEXT"
        ")"
    )
    conn.commit()

    applied = {
        row[0]: row[1]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    }

    newly: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        raw = path.read_text()
        checksum = _checksum(raw)
        if version in applied:
            if applied[version] and applied[version] != checksum:
                # Migration aplicada não pode mudar de conteúdo: corrija via nova migration.
                logger.warning(
                    "migration '%s' já aplicada mudou de conteúdo (%s != %s)",
                    version,
                    applied[version][:12],
                    checksum[:12],
                )
            continue
        for stmt in _split_statements(raw):
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)", (version, checksum)
        )
        conn.commit()
        newly.append(version)
    return newly


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Aplica as migrations do Turso (RF05)")
    parser.add_argument("--status", action="store_true", help="apenas listar o já aplicado")
    parser.add_argument("--database", help="URL/caminho alternativo (default: TURSO_DATABASE_URL)")
    args = parser.parse_args()

    try:
        conn = connect(args.database) if args.database else connect()
    except TursoNotConfigured as exc:
        print(f"erro: {exc}")
        return 2

    if args.status:
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            print("Nenhuma migration aplicada.")
        for version, applied_at in rows:
            print(f"  {version}  ({applied_at})")
        return 0

    newly = apply_migrations(conn)
    print(f"Aplicadas: {', '.join(newly)}" if newly else "Nada a aplicar — schema atualizado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
