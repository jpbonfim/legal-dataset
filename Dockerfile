# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/data/hf-cache

WORKDIR /app

# Dependencies first, so this layer is cached until the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# All generated artifacts (state, raw cache, parquet, logs, HF cache) live in this volume.
VOLUME /app/data

ENTRYPOINT ["legis-dataset"]
CMD ["--help"]
