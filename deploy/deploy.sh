#!/usr/bin/env bash
# Deploy do receiver na EC2 do Hermes (issue #13). Roda **na própria EC2**.
#
#   ./deploy.sh                 # sobe a tag do RECEIVER_IMAGE do .env (default: latest)
#   ./deploy.sh sha-abc123def   # fixa uma imagem específica (rollback usa isto)
#   ./deploy.sh --proxy         # sobe também o Caddy (profile `proxy`)
#
# O que ele garante, e por que cada passo existe:
#   1. `.env` presente e fechado (600) — sem ele o container sobe sem token e o endpoint
#      responde 503 de propósito (falha fechada);
#   2. `pull` antes de `up` — se o registry estiver fora, o serviço antigo continua no ar;
#   3. `/healthz` respondendo antes de declarar sucesso — container "Up" não é serviço de pé;
#   4. em caso de falha, mostra o log e a linha exata do rollback.
set -euo pipefail

cd "$(dirname "$0")"

PROXY=0
TAG=""
for arg in "$@"; do
	case "$arg" in
	--proxy) PROXY=1 ;;
	-h | --help)
		sed -n '2,12p' "$0"
		exit 0
		;;
	*) TAG="$arg" ;;
	esac
done

COMPOSE=(docker compose -f docker-compose.yml)
[ "$PROXY" = "1" ] && COMPOSE+=(--profile proxy)

# --- 1. pré-condições -----------------------------------------------------------------
[ -f .env ] || {
	echo "erro: .env ausente. Rode ./env-from-ssm.sh (docs/deploy.md §3)."
	exit 2
}
modo=$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env)
[ "$modo" = "600" ] || echo "aviso: .env com permissão $modo (esperado 600)"

if [ -n "$TAG" ]; then
	imagem="ghcr.io/cidlucas/ops-centro-receiver:${TAG}"
	export RECEIVER_IMAGE="$imagem"
	echo "→ imagem fixada: $imagem"
fi

anterior=$("${COMPOSE[@]}" images -q receiver 2>/dev/null | head -1 || true)

# --- 2. pull + up ----------------------------------------------------------------------
echo "→ baixando a imagem"
"${COMPOSE[@]}" pull receiver

echo "→ subindo"
"${COMPOSE[@]}" up -d --remove-orphans

# --- 3. o serviço está mesmo de pé? -----------------------------------------------------
echo -n "→ aguardando /healthz "
for _ in $(seq 1 30); do
	if docker exec ops-centro-receiver python -c \
		"import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" 2>/dev/null; then
		echo "ok"
		"${COMPOSE[@]}" ps
		echo
		echo "Pronto. Cheque o endpoint público: curl -fsS https://\$RECEIVER_DOMAIN/healthz"
		exit 0
	fi
	echo -n "."
	sleep 2
done

echo " FALHOU"
"${COMPOSE[@]}" logs --tail 50 receiver
echo
echo "Rollback: ./deploy.sh <tag-anterior>   (imagem anterior: ${anterior:-desconhecida})"
echo "As tags publicadas estão em https://github.com/CidLucas/ops-centro/pkgs/container/ops-centro-receiver"
exit 1
