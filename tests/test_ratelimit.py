"""What the rate limit is for, and what it should not be counting.

Two failures made it fire during ordinary use:

- Static assets shared the budget with the LLM-backed endpoints. A page load
  spends four or five requests on files that cost nothing to serve, so a few
  refreshes exhausted an allowance meant to protect generation.
- Behind Docker (and behind any reverse proxy) every client arrives with the
  same source address, so one bucket covered everybody. That is not a tuning
  problem — it is the limiter measuring the proxy instead of the caller.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from docquery.api.ratelimit import RateLimitMiddleware, client_key
from docquery.config import Settings, get_settings


def _app(settings: Settings, monkeypatch) -> TestClient:
    """A minimal app behind the real middleware.

    monkeypatch rather than a bare assignment: the middleware calls
    get_settings() at module scope, and replacing it permanently would leak
    into every test that runs afterwards — which it did, the first time.
    """
    import docquery.api.ratelimit as module

    monkeypatch.setattr(module, "get_settings", lambda: settings)

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/query")
    def query():
        return {"ok": True}

    @app.get("/assets/{name}")
    def asset(name: str):
        return {"asset": name}

    return TestClient(app)


def test_static_assets_do_not_spend_the_budget(monkeypatch):
    """The allowance exists to protect generation, not to ration a stylesheet."""
    client = _app(
        Settings(openai_api_key="sk-test", rate_limit_requests_per_minute=3),
        monkeypatch,
    )

    for _ in range(20):
        assert client.get("/assets/app.css").status_code == 200
    # The budget is untouched, so the endpoint that matters still answers.
    assert client.get("/query").status_code == 200


def test_the_limit_still_applies_to_real_endpoints(monkeypatch):
    """Control: without this the test above would pass on a broken limiter."""
    client = _app(
        Settings(openai_api_key="sk-test", rate_limit_requests_per_minute=3),
        monkeypatch,
    )

    codes = [client.get("/query").status_code for _ in range(5)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_a_forwarded_address_is_ignored_by_default():
    """X-Forwarded-For is caller-supplied, so trusting it by default would let
    anyone opt out of the limit by inventing a new address per request."""
    settings = Settings(openai_api_key="sk-test")

    assert client_key("10.0.0.1", "203.0.113.9", settings) == "10.0.0.1"


def test_a_forwarded_address_is_used_when_the_proxy_is_trusted():
    """Behind a proxy the socket address is the proxy's, identical for everyone.
    Keying on it throttles the whole company as one caller."""
    settings = Settings(openai_api_key="sk-test", rate_limit_trust_forwarded_for=True)

    assert client_key("10.0.0.1", "203.0.113.9", settings) == "203.0.113.9"


def test_only_the_first_hop_of_the_forwarded_chain_is_used():
    """The rest of the chain is whatever the caller chose to prepend."""
    settings = Settings(openai_api_key="sk-test", rate_limit_trust_forwarded_for=True)

    key = client_key("10.0.0.1", "203.0.113.9, 70.41.3.18, 150.172.238.178", settings)

    assert key == "203.0.113.9"


def test_a_blank_forwarded_header_falls_back_to_the_socket():
    settings = Settings(openai_api_key="sk-test", rate_limit_trust_forwarded_for=True)

    assert client_key("10.0.0.1", "", settings) == "10.0.0.1"
    assert client_key("10.0.0.1", None, settings) == "10.0.0.1"
