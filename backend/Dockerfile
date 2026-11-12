FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY docker ./docker
RUN uv sync --frozen --no-dev && chmod +x docker/entrypoint-api.sh \
    && useradd --system --uid 10001 --no-create-home smt \
    && chown -R smt /app

ENV PATH="/app/.venv/bin:$PATH"
USER smt
CMD ["python", "-m", "smtrading.worker.main"]
