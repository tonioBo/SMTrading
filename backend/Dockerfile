FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker ./docker
RUN uv sync --frozen --no-dev && chmod +x docker/entrypoint-api.sh \
    && useradd --system --uid 10001 --no-create-home smt \
    && mkdir -p /app/.cache && chown -R smt /app

# yfinance writes its tz cache under $HOME/.cache; the runtime user owns /app/.cache
ENV PATH="/app/.venv/bin:$PATH" HOME=/app
USER smt
CMD ["python", "-m", "smtrading.worker.main"]
