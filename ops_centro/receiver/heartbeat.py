"""Heartbeat do receiver — o batimento que torna o dead-man's switch possível (issue #27).

O receiver só emite métrica quando há alerta (`ops_centro_alerts_received_total` só aparece
num webhook), então em regime o Mimir não tem série nenhuma do `ops_centro_*` para uma regra
NoData observar — foi exatamente o que aconteceu em 05/08/2026, quando a EC2 emudeceu (sshd
aceitando TCP sem responder, Tailscale `Online=True` sem handshake) sem nenhum alerta, e só
um humano percebeu horas depois.

Este módulo emite um counter a cada 30s (env `HEARTBEAT_INTERVAL_SECONDS`): com a série
sempre presente no Mimir, a regra `ops-centro-host-deadman` dispara quando ela some
(NoData) **ou** congela (`increase` = 0) por 10m — roteada pela rota do #25 (Telegram nativo
do Grafana Cloud), que não depende da EC2 que parou de falar.

Mesma filosofia do resto do receiver: telemetria nunca derruba o serviço. Sem OTLP
configurado o incremento é no-op silencioso, e qualquer falha do SDK vira log de debug.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from ops_centro.conventions import APP_OPS_CENTRO
from ops_centro.metrics import common_labels

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30

# O texto espelha a entrada do catálogo (ops_centro/metrics.py): o instrumento OTel e o
# contrato as-code falam do mesmo nome e da mesma justificativa.
_HEARTBEAT_DESCRIPTION = (
    "Batimento do receiver a cada 30s — a série contínua que torna o dead-man's switch "
    "possível (issue #27): NoData ou increase=0 por >10m = EC2 emudeceu."
)

# Instrumento criado na primeira chamada de `start`, no mesmo padrão de `_alerts_counter`
# no app.py: o MeterProvider só existe depois do `setup_observability`, e sem OTLP
# configurado o SDK já é um no-op próprio.
_counter: object | None = None


def _interval_seconds() -> int:
    """Intervalo entre batimentos, da env `HEARTBEAT_INTERVAL_SECONDS` (default 30)."""
    raw = os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "")
    try:
        return int(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS


def _bater(
    counter: object,
    interval: int,
    sleep: Callable[[float], Awaitable[None]],
) -> Awaitable[None]:
    """Coroutine que incrementa o counter a cada `interval` segundos, para sempre.

    O `CancelledError` é consumido aqui dentro e vira retorno normal: a task termina sem
    exceção, e o asyncio não tem o que logar quando o lifespan derruba o batimento.
    """
    labels = {**common_labels(APP_OPS_CENTRO)}

    async def _loop() -> None:
        while True:
            try:
                await sleep(interval)
            except asyncio.CancelledError:
                return  # cancelamento limpo — vira retorno, não exceção
            try:
                counter.add(1, labels)
            except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba o receiver
                logger.debug("falha ao registrar o batimento do receiver: %s", exc)

    return _loop()


def start(
    interval: int = DEFAULT_INTERVAL_SECONDS,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> asyncio.Task | None:
    """Cria o counter OTel `ops_centro_heartbeat_total` e inicia a task que o incrementa.

    Devolve None em vez de levantar quando a telemetria não está pronta (mesmo padrão de
    `_record_alert` no app.py): um receiver que cai por causa do próprio observador não é
    um observador melhor.
    """
    global _counter
    if interval <= 0:
        interval = DEFAULT_INTERVAL_SECONDS
    try:
        if _counter is None:
            from opentelemetry import metrics as otel_metrics

            _counter = otel_metrics.get_meter("ops_centro.receiver.heartbeat").create_counter(
                "ops_centro_heartbeat_total",
                description=_HEARTBEAT_DESCRIPTION,
            )
        return asyncio.get_running_loop().create_task(_bater(_counter, interval, sleep))
    except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba o receiver
        logger.debug("heartbeat do receiver não iniciado (telemetria indisponível): %s", exc)
        return None


def stop(task: asyncio.Task | None) -> None:
    """Cancela a task de heartbeat, sem levantar.

    Não esperamos a task terminar aqui de propósito: numa task viva dentro do próprio loop,
    `task.result()` bloquearia o loop até o cancelamento ser processado. O `CancelledError`
    é consumido dentro de `_bater`, então o asyncio não loga exceção não recuperada.
    """
    if task is None or task.done():
        return
    task.cancel()
