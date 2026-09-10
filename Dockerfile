# syntax=docker/dockerfile:1
# Aim: Build a portable Gunicorn-based DATE Mapper runtime image.
# Author: Benjamin Turnbull

FROM python:3.11-slim-bookworm AS wheels

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt ./
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.11-slim-bookworm

ARG TARGETARCH
ARG TARGETPLATFORM

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=9099 \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=4 \
    GUNICORN_TIMEOUT=120

LABEL org.opencontainers.image.title="DATE Mapper" \
    io.date-mapper.target-architecture="${TARGETARCH}" \
    io.date-mapper.target-platform="${TARGETPLATFORM}"

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgdal32 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels \
    && python -c "import fiona, geopandas, pyogrio, pyproj, shapely"

COPY --chown=appuser:appuser app.py run_gunicorn.sh ./
COPY --chown=appuser:appuser Utilities ./Utilities
COPY --chown=appuser:appuser static ./static
COPY --chown=appuser:appuser templates ./templates
COPY --chown=appuser:appuser data ./data

RUN mkdir -p /app/.gunicorn /app/cache /app/storage \
    && chown appuser:appuser /app/.gunicorn /app/cache /app/storage \
    && chmod 0755 /app/run_gunicorn.sh

USER appuser

EXPOSE 9099

VOLUME ["/app/cache", "/app/storage"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9099/', timeout=4).read(1)"

CMD ["./run_gunicorn.sh"]
