# API image. Multi-arch by construction: the base is available for arm64, which
# is what the Hetzner CAX instances run (section 22.1).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies are installed from the manifest alone, so editing application
# code does not invalidate the layer.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install \
        "fastapi==0.121.2" "uvicorn[standard]==0.41.0" \
        "pydantic==2.12.4" "pydantic-settings==2.13.0" \
        "sqlalchemy[asyncio]==2.0.45" "asyncpg==0.31.0" \
        "alembic==1.17.1" "structlog==25.5.0" "sentry-sdk[fastapi]==2.44.0"

COPY apps/api /srv/apps/api
COPY packages /srv/packages
ENV PYTHONPATH=/srv/apps/api:/srv/packages

# Never run as root: a container escape should not start with uid 0.
RUN useradd --system --uid 10001 novabrief \
    && chown -R novabrief:novabrief /srv
USER novabrief

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
