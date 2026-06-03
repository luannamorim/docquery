"""Tests for document-type taxonomy: server-side classification + query filters.

Mirrors test_rbac.py's in-memory Qdrant approach (QdrantClient(":memory:")),
so no Docker is needed. The scoping filters (doc_types/source/tags) are applied
in the retrieval prefetch and must be ANDed with the clearance filter.
"""

import hashlib
from unittest.mock import patch

import numpy as np
import pytest
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
from docquery.ingest.loader import Document
from docquery.ingest.pipeline import _apply_type_policy
from docquery.retrieve.hybrid import retrieve

COLLECTION = "test_doc_type"
DIM = 8


def _settings(**overrides) -> Settings:
    defaults = {
        "qdrant_collection": COLLECTION,
        "embedding_dimension": DIM,
        "retrieval_top_k": 10,
        "openai_api_key": "sk-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _id(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)


def _point(text: str, source: str, doc_type: str, tags: list[str]) -> PointStruct:
    return PointStruct(
        id=_id(text),
        vector={
            "dense": [1.0] + [0.0] * (DIM - 1),
            "sparse": SparseVector(indices=[1, 2, 3], values=[0.5, 0.3, 0.2]),
        },
        payload={
            "text": text,
            "source": source,
            "chunk_index": 0,
            "file_type": ".md",
            "section": "",
            "clearance_level": 0,
            "doc_type": doc_type,
            "entity": "",
            "tags": tags,
        },
    )


@pytest.fixture()
def qdrant_client() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    client.upsert(
        collection_name=COLLECTION,
        points=[
            _point("Acme payment terms", "contracts/acme.md", "contract", ["acme"]),
            _point("Globex payment terms", "contracts/globex.md", "contract", []),
            _point("Security policy rules", "policies/sec.md", "policy", ["seguranca"]),
        ],
    )
    return client


def _retrieve(client, settings, **kwargs) -> set[str]:
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array([[1.0] + [0.0] * (DIM - 1)])
        mock_sparse.return_value = ([1, 2, 3], [0.5, 0.3, 0.2])
        points = retrieve("payment terms", client, settings, **kwargs)
    return {(p.payload or {}).get("source", "") for p in points}


def test_no_filter_returns_all_types(qdrant_client):
    sources = _retrieve(qdrant_client, _settings())
    assert sources == {
        "contracts/acme.md",
        "contracts/globex.md",
        "policies/sec.md",
    }


def test_doc_types_filter_restricts_to_type(qdrant_client):
    sources = _retrieve(qdrant_client, _settings(), doc_types=["contract"])
    assert sources == {"contracts/acme.md", "contracts/globex.md"}
    assert "policies/sec.md" not in sources


def test_source_filter_restricts_to_one_document(qdrant_client):
    sources = _retrieve(qdrant_client, _settings(), source="contracts/acme.md")
    assert sources == {"contracts/acme.md"}


def test_tags_filter_restricts_to_tagged_chunks(qdrant_client):
    sources = _retrieve(qdrant_client, _settings(), tags=["seguranca"])
    assert sources == {"policies/sec.md"}


def test_combined_filters_are_anded(qdrant_client):
    # contract type AND acme tag → only acme contract
    sources = _retrieve(
        qdrant_client, _settings(), doc_types=["contract"], tags=["acme"]
    )
    assert sources == {"contracts/acme.md"}


def test_apply_type_policy_classifies_by_prefix():
    settings = _settings(
        default_doc_type="document",
        type_policy=[("docs/contracts", "contract"), ("docs/policies", "policy")],
    )
    docs = [
        Document(content="x", metadata={"source": "docs/contracts/acme.md"}),
        Document(content="y", metadata={"source": "docs/policies/sec.md"}),
        Document(content="z", metadata={"source": "docs/manuals/setup.md"}),
    ]
    _apply_type_policy(docs, settings)
    assert docs[0].metadata["doc_type"] == "contract"
    assert docs[1].metadata["doc_type"] == "policy"
    assert docs[2].metadata["doc_type"] == "document"  # fallback to default


def test_api_propagates_scoping_filters():
    """The /query endpoint forwards doc_types/source/tags to the pipeline."""
    from fastapi.testclient import TestClient

    from docquery.api.app import app

    captured: dict = {}

    def _pipeline(query: str, settings=None, user_clearance: int = 0, **kwargs) -> dict:
        captured.update(kwargs)
        return {
            "answer": "test",
            "sources": [],
            "query": query,
            "model": "gpt-4o-mini",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
        }

    with patch("docquery.api.routes.query_pipeline", side_effect=_pipeline):
        client = TestClient(app)
        response = client.post(
            "/query",
            json={
                "query": "prazo de pagamento",
                "doc_types": ["contract"],
                "source": "docs/contracts/acme.md",
                "tags": ["acme"],
            },
        )

    assert response.status_code == 200
    assert captured.get("doc_types") == ["contract"]
    assert captured.get("source") == "docs/contracts/acme.md"
    assert captured.get("tags") == ["acme"]


def test_apply_type_policy_first_match_wins():
    settings = _settings(
        default_doc_type="document",
        type_policy=[
            ("docs/contracts/legacy", "legacy_contract"),
            ("docs/contracts", "contract"),
        ],
    )
    docs = [Document(content="x", metadata={"source": "docs/contracts/legacy/a.md"})]
    _apply_type_policy(docs, settings)
    assert docs[0].metadata["doc_type"] == "legacy_contract"
