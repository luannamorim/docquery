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
_EXEMPT_PATHS = frozenset({"/health"})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP rate limiter.

    Limit is taken from settings.rate_limit_requests_per_minute. A value <= 0
    disables the middleware entirely (useful in tests).
    """

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        limit = get_settings().rate_limit_requests_per_minute
        if limit <= 0:
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(ip, deque())
            while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= limit:
                return JSONResponse(
                    {"detail": "rate limit exceeded"}, status_code=429
                )
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
