# syntax=docker/dockerfile:1
# One backend image for every server-side role: api, scheduler, worker, migrate.
# The role is the first argument (see docker/entrypoint.sh). Built and run as non-root.
# Base images are pinned by digest; update them deliberately (docs/production_deployment.md).

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md ./
COPY src ./src
# An extra CA (only for build environments behind a TLS-inspecting proxy) can be passed as
# a BuildKit secret: --secret id=build_ca,src=ca.crt. It is never stored in an image layer.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --upgrade "pip>=24" && pip install .

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime
# postgresql-client provides pg_dump / pg_restore for `trend-agent system backup|restore-drill`.
# INSTALL_PG_CLIENT=0 builds without it (then run backups from a host with the client tools).
# Debian sources over HTTPS (apt also verifies signatures); optional build CA as above.
ARG INSTALL_PG_CLIENT=1
RUN --mount=type=secret,id=build_ca,required=false \
    if [ "$INSTALL_PG_CLIENT" = "1" ]; then \
      sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
      && CA="" && if [ -s /run/secrets/build_ca ]; then CA="-o Acquire::https::CAInfo=/run/secrets/build_ca"; fi \
      && apt-get $CA update \
      && apt-get $CA install -y --no-install-recommends postgresql-client \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TREND_AGENT_LOG_FORMAT=json \
    TREND_AGENT_DISCOVERY_CONFIG_DIR=/app/config \
    TREND_AGENT_OPERATIONS_CONFIG_DIR=/app/config
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app config ./config
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/trend-agent-entrypoint
RUN chmod 0755 /usr/local/bin/trend-agent-entrypoint && mkdir -p /app/backups /app/artifacts \
    && chown app:app /app/backups /app/artifacts
USER app
EXPOSE 8000
# Run with an init process (compose `init: true` / `docker run --init`) so signals are
# forwarded and zombies reaped; each role handles SIGTERM gracefully.
ENTRYPOINT ["/usr/local/bin/trend-agent-entrypoint"]
CMD ["api"]
