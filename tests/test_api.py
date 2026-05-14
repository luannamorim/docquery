from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from docquery.api.app import app
from docquery.config import Settings, get_settings

client = TestClient(app)


@pytest.fixture
def ingest_root(tmp_path):
    """Override settings.ingest_root to tmp_path for tests that POST /ingest."""
    app.dependency_overrides[get_settings] = lambda: Settings(ingest_root=tmp_path)
    try:
        yield tmp_path
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_success() -> None:
    mock_result = {
        "answer": "The answer is 42.",
        "sources": [
            {
                "index": 1,
                "source": "guide.md",
                "chunk_index": 0,
                "score": 9.5,
                "text": "The answer is 42.",
                "section": "Passo 1: Preparar",
            }
        ],
        "query": "What is the answer?",
        "model": "gpt-4o-mini",
    }
    with patch("docquery.api.routes.query_pipeline", return_value=mock_result):
        response = client.post("/query", json={"query": "What is the answer?"})
    assert response.status_code == 200
    data = response.json()
    assert data["answer"] == "The answer is 42."
    assert data["query"] == "What is the answer?"
    assert data["model"] == "gpt-4o-mini"
    assert len(data["sources"]) == 1
    assert data["sources"][0]["source"] == "guide.md"
    assert data["sources"][0]["section"] == "Passo 1: Preparar"


def test_query_source_section_defaults_to_empty() -> None:
    mock_result = {
        "answer": "ok",
        "sources": [
            {
                "index": 1,
                "source": "guide.md",
                "chunk_index": 0,
                "score": 1.0,
                "text": "something",
            }
        ],
        "query": "q",
        "model": "gpt-4o-mini",
    }
    with patch("docquery.api.routes.query_pipeline", return_value=mock_result):
        response = client.post("/query", json={"query": "q"})
    assert response.status_code == 200
    assert response.json()["sources"][0]["section"] == ""


def test_query_empty_body() -> None:
    response = client.post("/query", json={})
    assert response.status_code == 422


def test_query_missing_field() -> None:
    response = client.post("/query", json={"wrong_field": "test"})
    assert response.status_code == 422


def test_ingest_path_not_found() -> None:
    response = client.post("/ingest", json={"path": "/nonexistent/path"})
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_ingest_success(ingest_root) -> None:
    (ingest_root / "test.md").write_text("# Hello\n\nWorld.")
    mock_result = {"chunks": 3, "deleted": 0}
    with patch("docquery.api.routes.ingest_path", return_value=mock_result) as mock:
        response = client.post("/ingest", json={"path": str(ingest_root)})
    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "pending"
    mock.assert_called_once()


def test_ingest_path_outside_root_rejected(ingest_root, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "test.md").write_text("# Hello")
    response = client.post("/ingest", json={"path": str(outside)})
    assert response.status_code == 400
    assert "ingest_root" in response.json()["detail"]


def test_ingest_status_done(ingest_root) -> None:
    (ingest_root / "test.md").write_text("# Hello\n\nWorld.")
    mock_result = {"chunks": 3, "deleted": 0}
    with patch("docquery.api.routes.ingest_path", return_value=mock_result):
        post_response = client.post("/ingest", json={"path": str(ingest_root)})
    task_id = post_response.json()["task_id"]
    status_response = client.get(f"/ingest/{task_id}")
    assert status_response.status_code == 200
    data = status_response.json()
    assert data["task_id"] == task_id
    assert data["status"] == "done"
    assert data["chunks"] == 3
    assert data["deleted"] == 0
    assert data["error"] is None


def test_ingest_status_not_found() -> None:
    response = client.get("/ingest/nonexistent-task-id")
    assert response.status_code == 404


def test_ingest_empty_body() -> None:
    response = client.post("/ingest", json={})
    assert response.status_code == 422


def test_query_clearance_above_max_rejected() -> None:
    response = client.post(
        "/query",
        json={"query": "anything"},
        headers={"X-User-Clearance": "999"},
    )
    assert response.status_code == 400
    assert "X-User-Clearance" in response.json()["detail"]


def test_query_clearance_negative_rejected() -> None:
    response = client.post(
        "/query",
        json={"query": "anything"},
        headers={"X-User-Clearance": "-1"},
    )
    assert response.status_code == 400


def test_body_size_cap_rejects_oversized_request(monkeypatch) -> None:
    from docquery.api import ratelimit

    monkeypatch.setattr(
        ratelimit, "get_settings", lambda: Settings(request_max_body_bytes=100)
    )
    large = "x" * 200
    response = client.post("/query", json={"query": large})
    assert response.status_code == 413


def _walk_middleware_stack():
    """Yield each instance in the built ASGI middleware stack."""
    node = app.middleware_stack
    while node is not None:
        yield node
        node = getattr(node, "app", None)


def _reset_rate_limit_state() -> None:
    """Build the stack (via a single request) then clear the rate-limit deques."""
    from docquery.api import ratelimit

    client.get("/health")
    for mw in _walk_middleware_stack():
        if isinstance(mw, ratelimit.RateLimitMiddleware):
            mw._hits.clear()


def test_rate_limit_returns_429_when_exceeded(monkeypatch) -> None:
    from docquery.api import ratelimit

    monkeypatch.setattr(
        ratelimit, "get_settings", lambda: Settings(rate_limit_requests_per_minute=2)
    )
    _reset_rate_limit_state()
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/openapi.json").status_code == 429


def test_health_endpoint_exempt_from_rate_limit(monkeypatch) -> None:
    from docquery.api import ratelimit

    monkeypatch.setattr(
        ratelimit, "get_settings", lambda: Settings(rate_limit_requests_per_minute=1)
    )
    _reset_rate_limit_state()
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_security_headers_applied() -> None:
    response = client.get("/health")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Referrer-Policy") == "no-referrer"
    assert response.headers.get("Cache-Control") == "no-store"


def test_openapi_available() -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/health" in schema["paths"]
    assert "/query" in schema["paths"]
    assert "/ingest" in schema["paths"]
