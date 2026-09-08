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

COPY apps/api /srv/apps/api
COPY packages /srv/packages
ENV PYTHONPATH=/srv/apps/api:/srv/packages

# Never run as root: a container escape should not start with uid 0.
RUN useradd --system --uid 10001 novabrief \
    && chown -R novabrief:novabrief /srv
USER novabrief

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
