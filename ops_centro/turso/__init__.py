"""Acesso ao Turso: conexão, migrations, writer de logs (RF05) e retenção (issue #9).

Reexporta a API pública consumida pelos apps e pelo receiver:

    from ops_centro.turso import configure_log_writer, log_to_turso, shutdown_log_writer

O módulo de retenção (issue #9) **não** é importado aqui de propósito: ele é um job de
CLI (`python -m ops_centro.turso.retention`), e importá-lo no pacote faz o runpy
carregá-lo duas vezes ("found in sys.modules after import of package"). Quem precisa
dele importa direto de `ops_centro.turso.retention`.
"""

from ops_centro.turso.connection import TursoNotConfigured, connect, is_configured
from ops_centro.turso.log_writer import (
    TursoLogWriter,
    configure_log_writer,
    current_trace_id,
    get_log_writer,
    log_to_turso,
    normalize_level,
    shutdown_log_writer,
)

__all__ = [
    "TursoLogWriter",
    "TursoNotConfigured",
    "configure_log_writer",
    "connect",
    "current_trace_id",
    "get_log_writer",
    "is_configured",
    "log_to_turso",
    "normalize_level",
    "shutdown_log_writer",
]
