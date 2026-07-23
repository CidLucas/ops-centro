"""Artefatos as-code do Grafana: dashboards (issue #10) e, na fase 3, contact points.

Dashboard clicado na UI e não versionado se perde — e não é reproduzível num stack novo.
Por isso os JSONs de `grafana/dashboards/` são **gerados** a partir de
`ops_centro.grafana.dashboards`, que por sua vez monta os painéis em cima do catálogo de
métricas da §7 (`ops_centro.metrics`). Painel que cite métrica fora do catálogo quebra no
teste, em vez de virar um gráfico vazio em produção.
"""
