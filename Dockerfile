# Reproducible image for the shadow scanner.
#
# The container ships with live execution disabled and a zero daily spend ceiling.
# Those are defaults in the settings model too; setting them here as well means an
# image pulled without an env file still cannot spend money.

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv is copied from its official image rather than curl-piped into a shell, so the
# build has no network dependency on an install script.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer first: lockfile changes are rarer than source changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra dev

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY data/ ./data/
COPY alembic.ini tools/ ./
RUN uv sync --frozen --extra dev

# Run as a non-root user. Nothing here needs privileges.
RUN useradd --create-home --uid 10001 scanner \
    && mkdir -p /app/artifacts/evidence /app/artifacts/reports \
    && chown -R scanner:scanner /app
USER scanner

ENV PATH="/app/.venv/bin:$PATH" \
    TRADEUP_LIVE_EXECUTION_ENABLED=false \
    TRADEUP_MAX_DAILY_SPEND_MINOR=0 \
    TRADEUP_DATABASE_URL=sqlite+pysqlite:////app/artifacts/tradeup.db

# Fails if metadata, rules or the database are not wired up.
HEALTHCHECK --interval=60s --timeout=30s --start-period=10s --retries=3 \
    CMD tradeup doctor || exit 1

ENTRYPOINT []
CMD ["tradeup", "demo", "--quiet"]
