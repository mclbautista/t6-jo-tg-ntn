# JO Telegram Watchdog production image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dedicated non-root user (uid 1000). /app/data below is owned by it, and the named
# volume mounted there takes that ownership and mode on first use.
RUN groupadd --system --gid 1000 watchdog \
    && useradd --system --uid 1000 --gid watchdog --home-dir /app --shell /usr/sbin/nologin watchdog

# git installs the pinned t6-core release tag (pyproject.toml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY jo_watchdog ./jo_watchdog
RUN pip install . \
    && mkdir -p /app/data \
    && chown watchdog:watchdog /app/data \
    && chmod 0700 /app/data

USER watchdog

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "jo_watchdog.app", "healthcheck", "--config", "/app/config.yaml"]

ENTRYPOINT ["python", "-m", "jo_watchdog.app", "--config", "/app/config.yaml"]
