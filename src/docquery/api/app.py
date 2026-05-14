from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from docquery.api.routes import router
from docquery.config import get_settings
from docquery.retrieve.embedder import _get_model
from docquery.retrieve.reranker import _get_reranker


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach a small set of always-on response headers.

    No CORS or HSTS — those depend on deployment context (reverse proxy and
    whether the API is exposed over TLS); the README documents them as
    production considerations.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _get_model(settings.embedding_model)
    _get_reranker(settings.reranker_model)
    yield


app = FastAPI(title="docquery", version=version("docquery"), lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(router)
