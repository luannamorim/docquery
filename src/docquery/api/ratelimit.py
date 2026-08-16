"""In-memory rate limit and request-body-size middlewares.

Single-worker only. Multi-worker production deployments must move the
counters to an external store (Redis) — documented in SPEC.md as a
production consideration.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from docquery.config import get_settings

_RATE_LIMIT_WINDOW_SECONDS = 60.0
_EXEMPT_PATHS = frozenset({"/health", "/"})
#: Built assets. They cost nothing to serve and are requested several times per
#: page load, so counting them spends an allowance meant for the endpoint that
#: calls an LLM — a few refreshes could lock a user out of asking anything.
_EXEMPT_PREFIXES = ("/assets/",)


def client_key(peer: str, forwarded: str | None, settings) -> str:
    """Which caller a request counts against.

    The socket address is the honest answer only when the client dials us
    directly. Behind Docker's bridge, a reverse proxy or an ingress, every
    request arrives from the same address and one bucket ends up covering
    everybody — the limiter then measures the proxy, not the caller.

    X-Forwarded-For fixes that and is also caller-supplied, so honouring it by
    default would let anyone escape the limit by inventing an address per
    request. It is used only where an operator has said a trusted proxy sets
    it, and only its first hop: the rest of the chain is whatever the client
    chose to prepend.
    """
    if not settings.rate_limit_trust_forwarded_for:
        return peer
    first = (forwarded or "").split(",")[0].strip()
    return first or peer


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-caller rate limiter.

    Limit is taken from settings.rate_limit_requests_per_minute. A value <= 0
    disables the middleware entirely (useful in tests).
    """

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)
        settings = get_settings()
        limit = settings.rate_limit_requests_per_minute
        if limit <= 0:
            return await call_next(request)
        key = client_key(
            request.client.host if request.client else "unknown",
            request.headers.get("x-forwarded-for"),
            settings,
        )
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= limit:
                return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
            bucket.append(now)
        return await call_next(request)


class BodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length header exceeds the configured cap."""

    async def dispatch(self, request: Request, call_next):
        cap = get_settings().request_max_body_bytes
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > cap:
                    return JSONResponse(
                        {"detail": "request body too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid Content-Length"}, status_code=400
                )
        return await call_next(request)
