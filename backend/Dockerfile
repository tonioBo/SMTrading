FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# pg_dump 16 (must be >= the server's major) for the admin backup download; Debian's own
# postgresql-client is a newer major, so use the PGDG repository.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove curl gnupg && apt-get clean

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker ./docker
# «Istruzioni agenti» seed texts: the compose build context `agentdocs` = docs/paperclip-agents
COPY --from=agentdocs . ./agent-docs
ENV SMT_AGENT_DOCS_DIR=/app/agent-docs
RUN uv sync --frozen --no-dev && chmod +x docker/entrypoint-api.sh \
    && useradd --system --uid 10001 --no-create-home smt \
    && mkdir -p /app/.cache && chown -R smt /app

# build id (X-SMT-Build): set by compose; the web image stamps its own on proxied responses.
# SMT_BUILT_AT (optional, e.g. `date -Iseconds`): the build time on «Salute del sistema»
ARG SMT_BUILD_ID=dev
ARG SMT_BUILT_AT=
ENV SMT_BUILD_ID=$SMT_BUILD_ID SMT_BUILT_AT=$SMT_BUILT_AT

# yfinance writes its tz cache under $HOME/.cache; the runtime user owns /app/.cache
ENV PATH="/app/.venv/bin:$PATH" HOME=/app
USER smt
CMD ["python", "-m", "smtrading.worker.main"]
