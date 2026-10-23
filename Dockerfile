# oto-mcp — image déployable (SaaS pull-registry OU on-premise client).
# Tout par variables d'env (cf. §Config ci-dessous) : un client pointe sa
# propre DB, son auth OIDC, sa master key. Aucun secret dans l'image.
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
# L'installation PAR LE VERROU, celle de la production et de la CI (oto-backend#932) :
# `uv sync --frozen` pose exactement `uv.lock` — la lib connecteurs à l'épingle de
# `pyproject.toml`, jamais une branche flottante. La version d'uv est celle de
# `.github/actions/installer-par-le-verrou/action.yml` (garde :
# tests/test_image_par_le_verrou.py).
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/venv UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /src/oto-mcp
COPY . .
RUN uv sync --frozen --no-editable

FROM python:3.12-slim AS runtime
RUN useradd -r -u 10001 otomcp
COPY --from=builder /venv /venv
ENV PATH=/venv/bin:$PATH \
    MCP_TRANSPORT=streamable_http \
    HOST=0.0.0.0 \
    PORT=9103 \
    LOG_LEVEL=INFO
EXPOSE 9103
USER otomcp
# Config (env, jamais en dur) :
#   DATABASE_URL              PG (coffre + state). Le client fournit le sien.
#   LOGTO_ENDPOINT / MCP_AUDIENCE / OTO_MCP_PUBLIC_URL   auth OIDC.
#   OTO_MCP_OAUTH_STATE_SECRET   HMAC anti-CSRF.
#   OTO_MCP_ADMIN_SUB         sub admin bootstrap.
#   OTO_MCP_MASTER_KEY        chiffrement du coffre (hex64/base64-32o). Absente =
#                             dormant (secrets en clair). Le client la fournit
#                             depuis SON secret store.
ENTRYPOINT ["oto-mcp"]
