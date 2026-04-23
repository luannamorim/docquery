FROM python:3.12.11-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --no-install-project

COPY src/ src/
RUN uv sync --no-dev

# ---

FROM python:3.12.11-slim

WORKDIR /app

COPY --from=builder /app/.venv .venv
COPY src/ src/

RUN useradd --create-home appuser && mkdir -p eval/results && chown -R appuser:appuser eval/
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "docquery.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
