#!/usr/bin/env bash
# Monta o .env da EC2 a partir do AWS SSM Parameter Store (issue #13, RNF06).
#
# Segredo não vem em arquivo commitado nem em variável de ambiente do shell de quem
# deploia: vem do Parameter Store, cifrado, com o nome do parâmetro virando o nome da
# variável. Rotacionar = mudar o parâmetro e rodar isto de novo.
#
#   ./env-from-ssm.sh                      # /ops-centro/prod → ./.env
#   SSM_PREFIX=/ops-centro/staging ./env-from-ssm.sh /opt/ops-centro/.env
#
# Parâmetros esperados (SecureString para os segredos — ver docs/deploy.md §3):
#   /ops-centro/prod/ALERT_WEBHOOK_TOKEN     /ops-centro/prod/OTEL_EXPORTER_OTLP_ENDPOINT
#   /ops-centro/prod/TURSO_DATABASE_URL      /ops-centro/prod/OTEL_EXPORTER_OTLP_HEADERS
#   /ops-centro/prod/TURSO_AUTH_TOKEN        /ops-centro/prod/GRAFANA_STACK_URL
#   /ops-centro/prod/ENVIRONMENT             /ops-centro/prod/RECEIVER_DOMAIN ...
set -euo pipefail

PREFIX="${SSM_PREFIX:-/ops-centro/prod}"
DEST="${1:-$(cd "$(dirname "$0")" && pwd)/.env}"

command -v aws >/dev/null || { echo "erro: aws CLI não instalada"; exit 2; }

# umask antes de criar o arquivo: nem por um instante o .env pode ficar legível para
# outros usuários da máquina.
umask 077
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

{
	echo "# Gerado por deploy/env-from-ssm.sh a partir de $PREFIX em $(date -u +%FT%TZ)."
	echo "# NÃO editar à mão: a próxima execução sobrescreve. Mude no Parameter Store."
} >"$TMP"

# --recursive pega subcaminhos; --with-decryption resolve os SecureString.
aws ssm get-parameters-by-path \
	--path "$PREFIX" \
	--recursive \
	--with-decryption \
	--query 'Parameters[].[Name,Value]' \
	--output text |
	while IFS=$'\t' read -r nome valor; do
		# O valor vai entre aspas simples porque tokens OTLP contêm `%` e `=`; aspas
		# simples dentro do valor viram a sequência de escape do shell.
		printf "%s='%s'\n" "${nome##*/}" "${valor//\'/\'\\\'\'}"
	done >>"$TMP"

total=$(grep -c '^[A-Za-z_][A-Za-z0-9_]*=' "$TMP" || true)
if [ "$total" -eq 0 ]; then
	echo "erro: nenhum parâmetro em $PREFIX — confira o prefixo e a policy do IAM role"
	exit 1
fi

install -m 600 "$TMP" "$DEST"
echo "$DEST escrito com $total variável(is) de $PREFIX (modo 600)"
