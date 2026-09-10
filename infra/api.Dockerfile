# API image. Multi-arch by construction: the base is available for both arm64
# and x86, so the hosting decision of ADR-011 does not constrain it.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Installed from pyproject.toml rather than a list repeated here. That list was
# duplicated once and drifted immediately: two dependencies ended up in a
# developer environment and in neither manifest, which CI caught only because
# it builds from the file. Copying the manifest alone still keeps this layer
# cached when application code changes.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install .

# The interactive documentation, served from our own origin.
#
# FastAPI's default page pulls Swagger from a public CDN. That page lives on
# the same host as the authentication API, and it is where a person pastes an
# access token into the Authorize box - so a compromised or hijacked CDN would
# be running script in exactly the wrong place. Vendored here instead, pinned
# and checksummed, which also lets the Content-Security-Policy stay at 'self'
# rather than being opened up to a third party.
ARG SWAGGER_VERSION=5.17.14
ARG SWAGGER_JS_SHA256=c2e4a9ef08144839ff47c14202063ecfe4e59e70a4e7154a26bd50d880c88ba1
ARG SWAGGER_CSS_SHA256=40170f0ee859d17f92131ba707329a88a070e4f66874d11365e9a77d232f6117
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    mkdir -p /srv/static; \
    base="https://cdn.jsdelivr.net/npm/swagger-ui-dist@${SWAGGER_VERSION}"; \
    curl -fsSL "${base}/swagger-ui-bundle.js" -o /srv/static/swagger-ui-bundle.js; \
    curl -fsSL "${base}/swagger-ui.css" -o /srv/static/swagger-ui.css; \
    echo "${SWAGGER_JS_SHA256}  /srv/static/swagger-ui-bundle.js" | sha256sum -c -; \
    echo "${SWAGGER_CSS_SHA256}  /srv/static/swagger-ui.css" | sha256sum -c -; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

COPY apps/api /srv/apps/api
COPY packages /srv/packages
ENV PYTHONPATH=/srv/apps/api:/srv/packages

# Never run as root: a container escape should not start with uid 0.
RUN useradd --system --uid 10001 novabrief \
    && chown -R novabrief:novabrief /srv
USER novabrief

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
