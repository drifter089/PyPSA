# SPDX-FileCopyrightText: PyPSA Contributors
# SPDX-License-Identifier: MIT
# Build from the PyPSA repository root: docker build -f server/Dockerfile .
FROM python:3.12-slim-bookworm AS build

ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /src

# Git supplies setuptools_scm metadata for our LOCAL checkout.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.12.19

# Dockerfile.dockerignore excludes virtual environments, secrets, and large examples.
COPY . .
# --frozen preserves the third-party lock; local PyPSA is always built from /src.
# --no-editable makes the runtime independent of the source checkout.
RUN uv sync --project server --frozen --no-dev --no-editable

FROM build AS development
RUN uv sync --project server --frozen
WORKDIR /src/server
ENV PATH="/src/server/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 MPLBACKEND=Agg
EXPOSE 8000
CMD ["python", "-m", "app"]

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

ENV PATH="/src/server/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    PORT=8000 REQUEST_TIMEOUT_SECONDS=240

WORKDIR /src/server
COPY --from=build /src/server/.venv ./.venv
COPY --chown=appuser:appuser server/app ./app
COPY --chown=appuser:appuser examples/networks/ac-dc-meshed/ac-dc-meshed.nc /src/examples/networks/ac-dc-meshed/ac-dc-meshed.nc

USER appuser
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/healthz', timeout=3)"
CMD ["python", "-m", "app"]
