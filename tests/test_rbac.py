"""RBAC tests: clearance_level payload filter via X-User-Clearance header.

Uses QdrantClient(":memory:") for in-process Qdrant (no Docker needed).
Validates that chunks with clearance_level > user_clearance are never returned.
"""

import hashlib
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.retrieve.hybrid import retrieve

COLLECTION = "test_rbac"
DIM = 8  # small dimension for in-memory tests


def _settings(**overrides) -> Settings:
    defaults = {
        "qdrant_collection": COLLECTION,
        "embedding_dimension": DIM,
        "embedding_model": "all-MiniLM-L6-v2",
        "retrieval_top_k": 10,
        "openai_api_key": "sk-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_client_with_data() -> QdrantClient:
    """Create in-memory Qdrant with two chunks.

    public (clearance=0) and secret (clearance=5).
    """
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )

    def _id(text: str) -> int:
        return int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)

    public_text = "The hybrid search system uses dense and sparse vectors."
    secret_text = "Internal cost target is under $0.002 per query for gpt-4o-mini."

    # Dense vectors: simple unit vectors pointing in different directions
    public_dense = [1.0] + [0.0] * (DIM - 1)
    secret_dense = [0.0, 1.0] + [0.0] * (DIM - 2)

    client.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(
                id=_id(public_text),
                vector={
                    "dense": public_dense,
                    "sparse": SparseVector(indices=[1, 2, 3], values=[0.5, 0.3, 0.2]),
                },
                payload={
                    "text": public_text,
                    "source": "architecture.md",
                    "chunk_index": 0,
                    "file_type": ".md",
                    "section": "",
                    "clearance_level": 0,
                },
            ),
            PointStruct(
                id=_id(secret_text),
                vector={
                    "dense": secret_dense,
                    "sparse": SparseVector(indices=[4, 5, 6], values=[0.5, 0.3, 0.2]),
                },
                payload={
                    "text": secret_text,
                    "source": "internal_architecture.md",
                    "chunk_index": 0,
                    "file_type": ".md",
                    "section": "Internal Cost Targets",
                    "clearance_level": 5,
                },
            ),
        ],
    )
    return client


@pytest.fixture()
def qdrant_client():
    return _make_client_with_data()


@pytest.fixture()
def settings():
    return _settings()


def _get_texts(points) -> set[str]:
    return {(p.payload or {}).get("text", "") for p in points}


def test_clearance_0_never_returns_secret_chunk(qdrant_client, settings):
    query = "cost per query internal target"
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array([[0.1] * DIM])
        mock_sparse.return_value = (
            [4, 5, 6],
            [0.5, 0.3, 0.2],
        )  # matches secret chunk sparse

        points = retrieve(query, qdrant_client, settings, user_clearance=0)

    texts = _get_texts(points)
    assert not any("internal cost" in t.lower() for t in texts), (
        "Secret chunk (clearance=5) must not be returned for user_clearance=0"
    )


def test_clearance_5_returns_secret_chunk(qdrant_client, settings):
    query = "cost per query internal target"
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array(
            [[0.0, 1.0] + [0.0] * (DIM - 2)]
        )  # aligned with secret dense
        mock_sparse.return_value = ([4, 5, 6], [0.5, 0.3, 0.2])

        points = retrieve(query, qdrant_client, settings, user_clearance=5)

    texts = _get_texts(points)
    assert any("internal cost" in t.lower() for t in texts), (
        "Secret chunk (clearance=5) must be returned for user_clearance=5"
    )


def test_clearance_0_can_access_public_chunk(qdrant_client, settings):
    query = "hybrid search dense sparse vectors"
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array(
            [[1.0] + [0.0] * (DIM - 1)]
        )  # aligned with public dense
        mock_sparse.return_value = ([1, 2, 3], [0.5, 0.3, 0.2])

        points = retrieve(query, qdrant_client, settings, user_clearance=0)

    texts = _get_texts(points)
    assert any("hybrid search" in t.lower() for t in texts), (
        "Public chunk (clearance=0) must be accessible for user_clearance=0"
    )


def _make_capturing_pipeline() -> tuple[dict, callable]:
    """Return (captured, mock_fn) pair for testing clearance propagation."""
    captured: dict = {}

    def _pipeline(query: str, settings=None, user_clearance: int = 0) -> dict:
        captured["user_clearance"] = user_clearance
        return {
            "answer": "test",
            "sources": [],
            "query": query,
            "model": "gpt-4o-mini",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
        }

    return captured, _pipeline


def test_api_header_propagates_clearance():
    """Verify the X-User-Clearance header is read and passed to query_pipeline."""
    from docquery.api.app import app

    captured, mock_fn = _make_capturing_pipeline()
    with patch("docquery.api.routes.query_pipeline", side_effect=mock_fn):
        client = TestClient(app)
        response = client.post(
            "/query",
            json={"query": "what is hybrid search?"},
            headers={"X-User-Clearance": "3"},
        )

    assert response.status_code == 200
    assert captured.get("user_clearance") == 3


def test_api_default_clearance_is_zero():
    """Without the header, user_clearance defaults to 0."""
    from docquery.api.app import app

    captured, mock_fn = _make_capturing_pipeline()
    with patch("docquery.api.routes.query_pipeline", side_effect=mock_fn):
        client = TestClient(app)
        response = client.post("/query", json={"query": "what is hybrid search?"})

    assert response.status_code == 200
    assert captured.get("user_clearance") == 0
