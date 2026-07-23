# Imagem do receiver de alertas. Duas fases: builder resolve deps com uv
# (precisa de git — blu_observability_bootstrap vem do repo_platform via git),
# runtime roda como non-root (validado no smoke do CD).
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

# git é necessário para a dependência git+https do repo_platform
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Camada de deps separada do código para aproveitar cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ops_centro/ ops_centro/
COPY db/ db/
RUN uv sync --frozen --no-dev

# --- runtime -------------------------------------------------------------------
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 ops
WORKDIR /app

COPY --from=builder --chown=ops:ops /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER ops
EXPOSE 8080

CMD ["uvicorn", "ops_centro.receiver.app:app", "--host", "0.0.0.0", "--port", "8080"]
