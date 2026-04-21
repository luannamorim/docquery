FROM python:3.12.11-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends make && rm -rf /var/lib/apt/lists/*

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev deps, CPU-only torch)
ENV UV_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN uv sync --no-dev --no-install-project

# Copy source code, eval scripts and Makefile
COPY src/ src/
COPY eval/ eval/
COPY Makefile .
RUN uv sync --no-dev

# Run as non-root
RUN useradd --create-home appuser && mkdir -p eval/results && chown -R appuser:appuser eval/
USER appuser

EXPOSE 8000

CMD ["uv", "run", "fastapi", "run", "--host", "0.0.0.0"]
