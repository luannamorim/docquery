from unittest.mock import patch

from fastapi.testclient import TestClient

from docquery.api.app import app

client = TestClient(app)


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


def test_ingest_success(tmp_path) -> None:
    (tmp_path / "test.md").write_text("# Hello\n\nWorld.")
    mock_result = {"chunks": 3, "deleted": 0}
    with patch("docquery.api.routes.ingest_path", return_value=mock_result) as mock:
        response = client.post("/ingest", json={"path": str(tmp_path)})
    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "pending"
    mock.assert_called_once()


def test_ingest_status_done(tmp_path) -> None:
    (tmp_path / "test.md").write_text("# Hello\n\nWorld.")
    mock_result = {"chunks": 3, "deleted": 0}
    with patch("docquery.api.routes.ingest_path", return_value=mock_result):
        post_response = client.post("/ingest", json={"path": str(tmp_path)})
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


def test_openapi_available() -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/health" in schema["paths"]
    assert "/query" in schema["paths"]
    assert "/ingest" in schema["paths"]
