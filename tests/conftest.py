"""Keep the suite hermetic: it must not read the developer's own .env.

Settings loads .env by default, so any test that builds a bare Settings() —
or drives the app without overriding the dependency — inherits whatever the
machine happens to be configured with. That has broken this suite three times:
a leftover TYPE_POLICY, then the removed clearance keys, then AUTH_ENABLED=true
turning every unauthenticated request into a 401.

None of those were real failures, and worse, the reverse is possible: a test
asserting a closed door could pass only because the local .env happened to
close it. Pinning env_file to None here makes every test read the declared
defaults, the same on any machine and in CI.

Tests that need a configured value pass it explicitly, which they already do.
"""

import logging

import pytest

from docquery.config import Settings, get_settings


@pytest.fixture(autouse=True, scope="session")
def _ignore_developer_env_file() -> None:
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_docquery_logger():
    """Undo what the app's startup does to the shared `docquery` logger.

    configure_logging() sets propagate = False so our handler is the only one
    that prints, and it runs from the FastAPI lifespan — which means *any* test
    that builds a TestClient triggers it, not just the logging tests.

    pytest's caplog captures through a handler on the root logger, so once
    propagation is off, every later test asserting on a log record sees nothing
    and fails for a reason that has nothing to do with what it is testing.
    """
    logger = logging.getLogger("docquery")
    level, propagate, handlers = logger.level, logger.propagate, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.propagate = propagate
    logger.handlers = handlers


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Empty the shared rate-limit bucket after every test.

    The app object is a module-level singleton and its RateLimitMiddleware
    keeps hits in memory, keyed by peer address — which under TestClient is
    "testclient" for every request in the whole suite. One bucket for the
    whole run means each test spends allowance the tests after it needed, and
    the suite fails with 429s in whatever file happens to sort last.
    """
    yield
    from docquery.api.app import app
    from docquery.api.ratelimit import RateLimitMiddleware

    layer = app.middleware_stack
    while layer is not None:
        if isinstance(layer, RateLimitMiddleware):
            layer._hits.clear()
        layer = getattr(layer, "app", None)
