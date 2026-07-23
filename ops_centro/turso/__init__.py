"""Acesso ao Turso: conexão, migrations e writer de logs de longa retenção (RF05).

Reexporta a API pública consumida pelos apps e pelo receiver:

    from ops_centro.turso import configure_log_writer, log_to_turso, shutdown_log_writer
"""

from ops_centro.turso.connection import TursoNotConfigured, connect, is_configured
from ops_centro.turso.log_writer import (
    TursoLogWriter,
    configure_log_writer,
    current_trace_id,
    get_log_writer,
    log_to_turso,
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
    "shutdown_log_writer",
]
