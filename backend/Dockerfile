FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.27 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached layer), exactly as pinned in uv.lock, without the dev group.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 --no-create-home lararium
USER lararium

CMD ["uvicorn", "lararium.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
