# Frontend build. Its own stage so node never reaches the runtime image — the
# result is a directory of static files, and nothing that produced them is
# needed to serve them.
FROM node:22-slim AS frontend

WORKDIR /build

# Manifests first: dependencies only reinstall when they actually change, not
# on every edit to a component.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund

COPY frontend/ ./
# vite.config.ts writes to ../src/docquery/api/static, which is outside this
# stage's context — point it somewhere local and let the runtime stage place it.
RUN npx vite build --outDir dist --emptyOutDir

# ---

FROM python:3.12.11-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

# Docling's vision models import OpenCV, which links against these X/GL
# libraries even when running headless. Needed here as well as at runtime
# because the model prefetch below imports cv2.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Large wheels (torch ~180MB, scipy ~34MB) can exceed uv's default 30s
# download timeout on slower connections.
ENV UV_HTTP_TIMEOUT=300

COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --no-install-project

# Fetch Docling's model weights at build time so conversion never reaches the
# network at runtime. Only the models the pipeline actually uses: layout
# analysis, TableFormer for table structure, and RapidOCR for scanned pages.
#
# Deliberately BEFORE `COPY src/`. docling-tools is a console script from the
# docling dependency, so it exists as soon as the sync above finishes and this
# layer depends on nothing but pyproject.toml/uv.lock. Below the COPY, every
# edit to a single Python file would invalidate it and re-download hundreds of
# megabytes of weights — which is exactly what it used to do.
RUN /app/.venv/bin/docling-tools models download \
    -o /opt/docling-models layout tableformer rapidocr

COPY src/ src/
RUN uv sync --no-dev

# ---

FROM python:3.12.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv .venv
COPY --from=builder /opt/docling-models /opt/docling-models
COPY src/ src/
# The SPA, served by the API itself at "/" — same origin, so there is no CORS
# to configure and no second thing to deploy. app.py mounts this directory if
# it exists, so an image built without it still serves the API.
COPY --from=frontend /build/dist src/docquery/api/static

RUN useradd --create-home appuser \
    && mkdir -p eval/results /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser eval/ /home/appuser/.cache
USER appuser

ENV PATH="/app/.venv/bin:$PATH"
ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models

# Docling's layout model goes through torch.compile, which shells out to a C++
# compiler at runtime. The slim image has none, and every conversion would fail
# with InvalidCxxCompiler and silently fall back to the legacy parser. Running
# eager avoids that without shipping a toolchain, and also skips the
# per-process compilation cost — worth more here than compiled CPU kernels.
ENV TORCHDYNAMO_DISABLE=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "docquery.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
