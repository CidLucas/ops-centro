"""Testes do heartbeat do receiver (issue #27).

O dead-man's switch depende de uma série contínua no Mimir: este módulo emite um counter a
cada `interval` segundos para que a regra `ops-centro-host-deadman` tenha NoData para
observar quando a EC2 emudece. Estes testes garantem que o batimento incrementa, para de
incrementar no stop, não derruba o receiver sem OTel e lê o intervalo do ambiente.
"""

import asyncio

import pytest

from ops_centro.conventions import APP_OPS_CENTRO
from ops_centro.metrics import common_labels
from ops_centro.receiver import heartbeat as hb

pytestmark = pytest.mark.unit


def _meter_mock(mocker):
    """Meter falso: `create_counter` devolve um `counter.add` rastreável."""
    counter = mocker.Mock()
    meter = mocker.Mock()
    meter.create_counter.return_value = counter
    mocker.patch("opentelemetry.metrics.get_meter", return_value=meter)
    mocker.patch.object(hb, "_counter", None)
    return counter


async def test_heartbeat_incrementa_o_counter_em_intervalos(mocker):
    """Com intervalo minúsculo, a task incrementa o counter várias vezes, sempre com as
    labels comuns do RF02 (`app_name`/`environment`)."""
    counter = _meter_mock(mocker)
    task = hb.start(interval=0.005)
    assert task is not None
    await asyncio.sleep(0.02)  # ~4 intervalos de 5ms
    hb.stop(task)
    await asyncio.sleep(0)  # deixa o loop processar o cancelamento
    labels = {**common_labels(APP_OPS_CENTRO)}
    assert counter.add.call_count >= 2
    for chamada in counter.add.call_args_list:
        assert chamada.args == (1, labels)


async def test_heartbeat_stop_cancela_a_task(mocker):
    """`stop()` numa task viva cancela sem exceção; repetir em task encerrada (ou None) é
    no-op — o lifespan derruba a task no shutdown sem ruído."""
    _meter_mock(mocker)
    task = hb.start(interval=3600)  # intervalo longo: nunca incrementa no teste
    assert task is not None
    assert not task.done()
    hb.stop(task)
    await asyncio.sleep(0)  # deixa o loop processar o cancelamento
    assert task.cancelled()
    hb.stop(task)
    hb.stop(None)


def test_heartbeat_sem_otel_nao_levanta(mocker):
    """Sem SDK OTel (ou com ele falhando), `start()` devolve None e nada propaga — a
    telemetria nunca derruba o receiver."""
    mocker.patch(
        "opentelemetry.metrics.get_meter", side_effect=RuntimeError("SDK não pronto")
    )
    mocker.patch.object(hb, "_counter", None)
    assert hb.start(interval=0.01) is None


def test_intervalo_vem_do_env(monkeypatch):
    """O deploy ajusta a cadência sem rebuild: `HEARTBEAT_INTERVAL_SECONDS` no ambiente."""
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "5")
    assert hb._interval_seconds() == 5
    monkeypatch.delenv("HEARTBEAT_INTERVAL_SECONDS")
    assert hb._interval_seconds() == hb.DEFAULT_INTERVAL_SECONDS
