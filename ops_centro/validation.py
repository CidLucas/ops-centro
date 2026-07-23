"""Validação da chegada de sinais no Grafana Cloud — gate da Fase 1 (issue #6).

Executa, de forma repetível, o checklist de `docs/validacao-fase1.md`: métricas no
Mimir/Prometheus, logs no Loki e traces no Tempo, todos carregando os atributos comuns
do RF02 e cruzáveis por `app_name`/`tenant_id` (RNF05).

    uv run python -m ops_centro.validation            # checklist completo
    uv run python -m ops_centro.validation --app agents-platform
    uv run python -m ops_centro.validation --json     # saída para o baseline

Credenciais (ver docs/secrets.md): as de escrita (`OTEL_EXPORTER_OTLP_*`) **não** servem
para consulta — é preciso um token de leitura (`metrics:read`, `logs:read`, `traces:read`)
e as URLs dos datasources do stack:

    GRAFANA_READ_TOKEN + GRAFANA_STACK_URL                    (token glsa_, via proxy)
    GRAFANA_READ_TOKEN + GRAFANA_{PROM,LOKI,TEMPO}_{URL,USER}  (Access Policy, direto)

Um token `glsa_` (service account) fala com a instância Grafana e alcança os
datasources pelo proxy dela — basta a URL do stack. Um token de Access Policy fala
direto com cada datasource, e aí são precisas as três URLs e os três user ids.

Cada checagem imprime a query que rodou: o mesmo texto pode ser colado no Explore para
conferência manual, e é isso que torna o resultado auditável.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import httpx

from ops_centro.conventions import (
    APP_AGENTS_PLATFORM,
    APP_FILE_MEMORY,
    APP_OPS_CENTRO,
    ATTR_APP_NAME,
    ATTR_ENVIRONMENT,
    ATTR_TENANT_ID,
    ATTR_VERSION,
    METRIC_PREFIXES,
)

DEFAULT_APPS = (APP_AGENTS_PLATFORM, APP_FILE_MEMORY, APP_OPS_CENTRO)
DEFAULT_WINDOW = "1h"
TIMEOUT = httpx.Timeout(30.0)


@dataclass
class CheckResult:
    """Resultado de uma checagem do roteiro."""

    signal: str  # metrics | logs | traces
    name: str
    query: str
    ok: bool
    detail: str
    skipped: bool = False

    def line(self) -> str:
        mark = "SKIP" if self.skipped else ("PASS" if self.ok else "FALHA")
        return f"[{mark}] {self.signal:<7} {self.name}\n         query: {self.query}\n         {self.detail}"


@dataclass
class Endpoint:
    """Datasource do Grafana Cloud: URL base + usuário de basic auth.

    Caminho direto (token de Access Policy com `*:read`): cada datasource tem URL e
    user id próprios e a auth é basic `user:token`.
    """

    url: str
    user: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.url and self.user and self.token)

    def get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        return httpx.get(
            f"{self.url.rstrip('/')}{path}",
            params=params,
            auth=(self.user, self.token),
            timeout=TIMEOUT,
        )


@dataclass
class ProxyEndpoint:
    """Datasource alcançado pelo proxy da instância Grafana (token `glsa_`).

    Um service account token não fala com os datasources direto — fala com a
    instância (`https://<slug>.grafana.net`), que repassa a query pelo proxy em
    `/api/datasources/proxy/uid/<uid>`. O uid é descoberto uma vez em
    `/api/datasources`, filtrando pelo tipo (prometheus | loki | tempo).
    """

    base_url: str
    token: str
    ds_type: str
    _uid: str | None = None
    _erro: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def uid(self) -> str | None:
        """uid do primeiro datasource do tipo esperado (cacheado)."""
        if self._uid or self._erro:
            return self._uid
        try:
            response = httpx.get(
                f"{self.base_url.rstrip('/')}/api/datasources",
                headers=self._headers(),
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            self._erro = f"erro de rede ao listar datasources: {exc}"
            return None
        if response.status_code != 200:
            self._erro = f"HTTP {response.status_code} em /api/datasources: {response.text[:150]}"
            return None
        for datasource in response.json():
            if datasource.get("type") == self.ds_type:
                self._uid = datasource["uid"]
                return self._uid
        self._erro = f"nenhum datasource do tipo {self.ds_type} na instância"
        return None

    def get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        uid = self.uid()
        if uid is None:
            request = httpx.Request("GET", f"{self.base_url}{path}")
            return httpx.Response(502, text=self._erro, request=request)
        return httpx.get(
            f"{self.base_url.rstrip('/')}/api/datasources/proxy/uid/{uid}{path}",
            params=params,
            headers=self._headers(),
            timeout=TIMEOUT,
        )


@dataclass
class GrafanaCloud:
    """Os três datasources de consulta do stack."""

    prom: Endpoint | ProxyEndpoint
    loki: Endpoint | ProxyEndpoint
    tempo: Endpoint | ProxyEndpoint

    @classmethod
    def from_env(cls) -> "GrafanaCloud":
        """Escolhe o modo de acesso pelo tipo do token.

        `glsa_...` (service account) → proxy da instância, que só precisa de
        `GRAFANA_STACK_URL`. Qualquer outro → caminho direto por datasource, com as
        URLs e user ids de cada um.
        """
        token = os.environ.get("GRAFANA_READ_TOKEN", "")
        stack_url = os.environ.get("GRAFANA_STACK_URL", "")
        if token.startswith("glsa_"):
            return cls(
                prom=ProxyEndpoint(stack_url, token, "prometheus"),
                loki=ProxyEndpoint(stack_url, token, "loki"),
                tempo=ProxyEndpoint(stack_url, token, "tempo"),
            )
        return cls(
            prom=Endpoint(os.environ.get("GRAFANA_PROM_URL", ""), os.environ.get("GRAFANA_PROM_USER", ""), token),
            loki=Endpoint(os.environ.get("GRAFANA_LOKI_URL", ""), os.environ.get("GRAFANA_LOKI_USER", ""), token),
            tempo=Endpoint(os.environ.get("GRAFANA_TEMPO_URL", ""), os.environ.get("GRAFANA_TEMPO_USER", ""), token),
        )


def _skip(signal: str, name: str, query: str, motivo: str) -> CheckResult:
    return CheckResult(signal, name, query, ok=False, detail=motivo, skipped=True)


def _request(
    endpoint: Endpoint | ProxyEndpoint,
    signal: str,
    name: str,
    path: str,
    params: dict[str, Any],
    evaluate: Callable[[dict], tuple[bool, str]],
    query_text: str,
) -> CheckResult:
    """Roda uma consulta e delega a interpretação do corpo a `evaluate`.

    Erro de rede/HTTP vira FALHA com a mensagem do backend — nunca exceção: o roteiro
    precisa terminar e mostrar TODAS as checagens, não parar na primeira.
    """
    if not endpoint.configured:
        return _skip(signal, name, query_text, "datasource não configurado (ver docs/secrets.md)")
    try:
        response = endpoint.get(path, params)
    except httpx.HTTPError as exc:
        return CheckResult(signal, name, query_text, False, f"erro de rede: {exc}")
    if response.status_code != 200:
        return CheckResult(
            signal, name, query_text, False, f"HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError:
        return CheckResult(signal, name, query_text, False, "resposta não é JSON")
    ok, detail = evaluate(body)
    return CheckResult(signal, name, query_text, ok, detail)


# --- métricas (Prometheus/Mimir) ----------------------------------------------
def check_metrics(cloud: GrafanaCloud, app: str, window: str = DEFAULT_WINDOW) -> list[CheckResult]:
    """Séries do app existem e carregam os atributos comuns do RF02."""
    prefix = METRIC_PREFIXES[app]
    series_query = f'count(count by (__name__) ({{__name__=~"{prefix}_.*"}}))'
    rf02_query = (
        f'count by ({ATTR_APP_NAME}, {ATTR_ENVIRONMENT}, {ATTR_VERSION}) '
        f'({{{ATTR_APP_NAME}="{app}"}})'
    )

    def _series(body: dict) -> tuple[bool, str]:
        value = _scalar(body)
        return (value or 0) > 0, f"{int(value or 0)} série(s) com prefixo {prefix}_"

    def _rf02(body: dict) -> tuple[bool, str]:
        results = body.get("data", {}).get("result", [])
        combos = [r.get("metric", {}) for r in results]
        faltando = [
            attr
            for attr in (ATTR_APP_NAME, ATTR_ENVIRONMENT, ATTR_VERSION)
            if not all(combo.get(attr) for combo in combos)
        ]
        if not combos:
            return False, f"nenhuma série com {ATTR_APP_NAME}={app}"
        if faltando:
            return False, f"atributos ausentes em alguma série: {', '.join(faltando)}"
        return True, f"{len(combos)} combinação(ões) app/environment/version: {combos}"

    return [
        _request(cloud.prom, "métricas", f"séries de {app}", "/api/v1/query",
                 {"query": series_query}, _series, series_query),
        _request(cloud.prom, "métricas", f"RF02 em {app}", "/api/v1/query",
                 {"query": rf02_query}, _rf02, rf02_query),
    ]


def _scalar(body: dict) -> float | None:
    """Extrai o valor de um resultado instantâneo escalar/vetorial de 1 série."""
    results = body.get("data", {}).get("result", [])
    if not results:
        return 0.0
    value = results[0].get("value")
    try:
        return float(value[1])
    except (TypeError, IndexError, ValueError):
        return None


# --- logs (Loki) ---------------------------------------------------------------
def check_logs(cloud: GrafanaCloud, app: str, window: str = DEFAULT_WINDOW) -> list[CheckResult]:
    """Logs do app chegam no Loki com a label `app_name`."""
    query = f'sum(count_over_time({{{ATTR_APP_NAME}="{app}"}}[{window}]))'

    def _evaluate(body: dict) -> tuple[bool, str]:
        results = body.get("data", {}).get("result", [])
        total = 0.0
        for serie in results:
            valores = serie.get("values") or ([serie["value"]] if "value" in serie else [])
            for _ts, valor in valores:
                total += float(valor)
        return total > 0, f"{int(total)} linha(s) de log em {window}"

    return [
        _request(cloud.loki, "logs", f"logs de {app}", "/loki/api/v1/query",
                 {"query": query}, _evaluate, query)
    ]


# --- traces (Tempo) ------------------------------------------------------------
def check_traces(cloud: GrafanaCloud, app: str, window: str = DEFAULT_WINDOW) -> list[CheckResult]:
    """Traces do app chegam no Tempo e 100% deles carregam os atributos do RF02.

    O critério de aceite do #3 é exatamente este: a busca com os três atributos
    presentes tem que devolver o mesmo tanto que a busca só por `app_name`.
    """
    base = f'{{resource.{ATTR_APP_NAME}="{app}"}}'
    completo = (
        f'{{resource.{ATTR_APP_NAME}="{app}" && resource.{ATTR_ENVIRONMENT}!=nil '
        f'&& resource.{ATTR_VERSION}!=nil}}'
    )

    def _tem_traces(body: dict) -> tuple[bool, str]:
        traces = body.get("traces") or []
        return len(traces) > 0, f"{len(traces)} trace(s) na janela"

    resultados = [
        _request(cloud.tempo, "traces", f"traces de {app}", "/api/search",
                 {"q": base, "limit": 20}, _tem_traces, base)
    ]

    def _rf02(body: dict) -> tuple[bool, str]:
        com_atributos = len(body.get("traces") or [])
        total = _contagem_traces(cloud, base)
        if total is None:
            return com_atributos > 0, f"{com_atributos} trace(s) com RF02 completo"
        if total == 0:
            return False, "nenhum trace na janela — dispare uma execução no dev antes"
        return (
            com_atributos == total,
            f"{com_atributos}/{total} traces com app_name+environment+version",
        )

    resultados.append(
        _request(cloud.tempo, "traces", f"RF02 em {app}", "/api/search",
                 {"q": completo, "limit": 20}, _rf02, completo)
    )
    return resultados


def _contagem_traces(cloud: GrafanaCloud, query: str) -> int | None:
    """Total de traces de uma busca (None se a consulta falhar)."""
    try:
        response = cloud.tempo.get("/api/search", {"q": query, "limit": 20})
        if response.status_code != 200:
            return None
        return len(response.json().get("traces") or [])
    except (httpx.HTTPError, ValueError):
        return None


def check_cross_app(cloud: GrafanaCloud, apps: list[str]) -> list[CheckResult]:
    """Query cruzada por `tenant_id` entre os apps — é o que valida o RNF05 na prática."""
    query = f'{{span.{ATTR_TENANT_ID}!=nil}}'

    def _evaluate(body: dict) -> tuple[bool, str]:
        traces = body.get("traces") or []
        nomes = {t.get("rootServiceName") or t.get("rootTraceName", "?") for t in traces}
        return len(traces) > 0, f"{len(traces)} trace(s) com tenant_id; serviços: {sorted(nomes)}"

    return [
        _request(cloud.tempo, "traces", "cruzamento por tenant_id", "/api/search",
                 {"q": query, "limit": 20}, _evaluate, query)
    ]


def run_checklist(
    cloud: GrafanaCloud, apps: list[str] | None = None, window: str = DEFAULT_WINDOW
) -> list[CheckResult]:
    """Roteiro completo do #6 para os apps indicados."""
    apps = apps or list(DEFAULT_APPS)
    resultados: list[CheckResult] = []
    for app in apps:
        resultados += check_metrics(cloud, app, window)
        resultados += check_logs(cloud, app, window)
        resultados += check_traces(cloud, app, window)
    resultados += check_cross_app(cloud, apps)
    return resultados


@dataclass
class Summary:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[CheckResult] = field(default_factory=list)

    @classmethod
    def of(cls, results: list[CheckResult]) -> "Summary":
        return cls(
            passed=sum(1 for r in results if r.ok and not r.skipped),
            failed=sum(1 for r in results if not r.ok and not r.skipped),
            skipped=sum(1 for r in results if r.skipped),
            results=results,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Valida os sinais no Grafana Cloud (issue #6)")
    parser.add_argument("--app", action="append", choices=list(DEFAULT_APPS),
                        help="restringe a um app (pode repetir; default: todos)")
    parser.add_argument("--window", default=DEFAULT_WINDOW, help="janela das consultas (default 1h)")
    parser.add_argument("--json", action="store_true", help="saída JSON (para o baseline)")
    args = parser.parse_args(argv)

    cloud = GrafanaCloud.from_env()
    summary = Summary.of(run_checklist(cloud, args.app, args.window))

    if args.json:
        print(json.dumps({
            "passed": summary.passed, "failed": summary.failed, "skipped": summary.skipped,
            "results": [asdict(r) for r in summary.results],
        }, ensure_ascii=False, indent=2))
    else:
        for result in summary.results:
            print(result.line())
        print(f"\n{summary.passed} ok · {summary.failed} falha(s) · {summary.skipped} pulada(s)")
        if summary.skipped:
            print("Checagens puladas exigem token de LEITURA do Grafana Cloud — docs/secrets.md.")

    # Pulado não reprova: sem credenciais de leitura o gate simplesmente não foi executado.
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
