import logging
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from docquery.api.auth import _get_jwks_client, jwks_uri_for
from docquery.api.ratelimit import BodySizeMiddleware, RateLimitMiddleware
from docquery.api.routes import router, system_router
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


logger = logging.getLogger(__name__)


def configure_logging(settings) -> None:
    """Make this package's own log lines reach the operator.

    uvicorn configures `uvicorn.*` and leaves the root logger at WARNING with no
    handler, so every logger.info here is discarded. The line that goes missing
    matters more than most:

        Query authorized for sectors=['contracts']

    Without it, "the answer was empty" and "your roles reach nothing" look
    identical from the outside, which is exactly the question an operator needs
    the log to settle.

    Configured on the `docquery` logger rather than the root so uvicorn's access
    log keeps its own formatting, and idempotent because the lifespan can run
    more than once in a process (reloads, tests).
    """
    logger_ = logging.getLogger("docquery")
    logger_.setLevel(settings.log_level)
    if not logger_.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(name)s - %(message)s")
        )
        logger_.addHandler(handler)
    # Ours are emitted by our handler; propagating as well would print each
    # line twice once anything configures the root.
    logger_.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    _get_model(settings.embedding_model)
    _get_reranker(settings.reranker_model)
    if settings.auth_enabled:
        # Best-effort: warm the JWKS cache so the first authenticated request
        # doesn't pay the fetch. Never fatal — a tenant that is briefly
        # unreachable must not stop the container from serving /health.
        try:
            _get_jwks_client(jwks_uri_for(settings)).get_jwk_set()
        except Exception as exc:
            logger.warning("Could not prefetch JWKS at startup: %s", exc)
    else:
        logger.warning(
            "AUTH_ENABLED is false — the API is running WITHOUT authentication "
            "and retrieval is unrestricted. Do not use in production."
        )
    yield


app = FastAPI(title="docquery", version=version("docquery"), lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeMiddleware)
app.include_router(system_router)
app.include_router(router)


def mount_frontend(app: FastAPI, directory: Path) -> bool:
    """Serve the built SPA at /, if it has been built. Returns whether it was.

    Mounted last and deliberately: a mount at "/" is a catch-all, so it must be
    registered after every router or it would swallow /query and /health.

    Serving the frontend from the API is what keeps CORS out of this app — same
    origin means no preflight and no allowed-origins list to keep in sync per
    environment. It also keeps `docker compose up` a single deployable.

    Conditional because a checkout has no build: CI, the test suite and the eval
    runner all import this module, and none of them run npm.
    """
    index = directory / "index.html"
    if not index.is_file():
        return False
    app.mount("/", StaticFiles(directory=directory, html=True), name="frontend")
    logger.info("Serving frontend from %s", directory)
    return True


# Built by the Dockerfile's node stage into the image; absent in a checkout.
mount_frontend(app, Path(__file__).parent / "static")
