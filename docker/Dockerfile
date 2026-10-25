# Self-contained runtime image (constraint C1: the host ever needs only Docker).
# uv manages the interpreter and every dependency; nothing is installed on the host.
# The `python3.12-bookworm-slim` tag is the Debian-based image (has /bin/sh,
# uv, and CPython 3.12) — the plain `uv:latest` tag is distroless (no shell).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Cache lives outside /app (the scanned workdir) and is owned by the runtime
# user below, so the non-root process can write to it.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/cache/uv

WORKDIR /app

# Dependencies first (layer caching); the lockfile pins exact versions.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Application source + tests (tests run in-container: `make test`).
COPY src ./src
COPY tests ./tests

# Non-root runtime user; data dir hosts the SQLite DB + event log (M2+).
RUN useradd -u 10001 -s /bin/false -d /app rpg \
    && mkdir -p /app/data /cache/uv \
    && chown -R rpg:rpg /app /cache/uv
USER rpg

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "rpg.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
