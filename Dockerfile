FROM python:3.12-slim

# Install uv — fast Python package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Prevent .pyc files
ENV PYTHONDONTWRITEBYTECODE=1
# Flush stdout/stderr immediately so logs aren't lost on crash
ENV PYTHONUNBUFFERED=1
# Pre-compile bytecode for faster cold-start
ENV UV_COMPILE_BYTECODE=1
# Copy files instead of symlinking (required when source is a mounted volume)
ENV UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends gcc build-essential curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --shell "/sbin/nologin" \
    --uid "${UID}" \
    appuser

# Install dependencies from the lockfile first (separate layer for Docker cache efficiency).
# This layer is only rebuilt when pyproject.toml or uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Copy application source and install the project itself
ADD --chown=appuser:0 . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Put the virtualenv's executables on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Create data directory for SQLite persistence
RUN mkdir -p /app/data && chown appuser:0 /app/data

USER appuser

EXPOSE 9000

CMD ["gunicorn", "payroll.app:app", "-c", "payroll/gunicorn_config.py"]
