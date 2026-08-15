"""Authorization tests covering the Docling ingestion path.

The sector filter is the one thing that must never regress: an unauthorized
chunk reaching the context is a data leak, not a quality problem. These tests
drive a real in-memory Qdrant so the filters are actually evaluated, and each
negative test is paired with a positive control proving the restricted chunk is
reachable when the sector allows it — without the control, a broken query that
returns nothing would make the negative assertion pass for the wrong reason.
"""

import hashlib
from unittest.mock import patch

import numpy as np
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.ingest.chunker import Chunk
from docquery.ingest.pipeline import ingest_chunks
from docquery.retrieve.expand import expand_contexts
from docquery.retrieve.hybrid import retrieve

COLLECTION = "test_docling_rbac"
DIM = 8

PUBLIC_INTRO = "The runbook explains how to restart the ingestion service."
SECRET_TEXT = "The rollback token for the staging cluster is RBK-7781-ZULU."
PUBLIC_TAIL = "Afterwards, confirm the readiness probe reports a healthy status."


def _settings(**overrides) -> Settings:
    defaults = {
        "qdrant_collection": COLLECTION,
        "embedding_dimension": DIM,
        "retrieval_top_k": 10,
        "openai_api_key": "sk-test",
        "docling_enabled": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _fake_dense(texts: list[str], **_kwargs) -> np.ndarray:
    """Deterministic unit vectors so retrieval is reproducible without a model."""
    out = []
    for text in texts:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        vec = np.zeros(DIM, dtype=np.float32)
        vec[seed % DIM] = 1.0
        out.append(vec)
    return np.vstack(out)


def _docling_chunks() -> list[Chunk]:
    """Chunks shaped exactly as the Docling path produces them."""
    common = {
        "source": "docs/runbook.pdf",
        "file_type": ".pdf",
        "folders": ["docs"],
        "title": "Deployment Runbook",
        "content_type": "text",
    }
    return [
        Chunk(
            text=PUBLIC_INTRO,
            metadata={
                **common,
                "chunk_index": 0,
                "section": "Runbook > Overview",
                "page_number": 1,
                "sector": "docs",
            },
        ),
        Chunk(
            text=SECRET_TEXT,
            metadata={
                **common,
                "chunk_index": 1,
                "section": "Runbook > Rollback",
                "page_number": 2,
                "sector": "restrito",
            },
        ),
        Chunk(
            text=PUBLIC_TAIL,
            metadata={
                **common,
                "chunk_index": 2,
                "section": "Runbook > Verification",
                "page_number": 2,
                "sector": "docs",
            },
        ),
    ]


def _client_with_docling_chunks() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    settings = _settings()
    with patch("docquery.ingest.pipeline.embed_texts", side_effect=_fake_dense):
        ingest_chunks(_docling_chunks(), client, settings)
    return client


@pytest.fixture()
def client():
    return _client_with_docling_chunks()


def _payload_for(client: QdrantClient, text: str) -> dict:
    points, _ = client.scroll(collection_name=COLLECTION, limit=100, with_payload=True)
    for point in points:
        if (point.payload or {}).get("text") == text:
            return point.payload
    raise AssertionError(f"no point found with text {text!r}")


# --- G/H: payload written by the Docling path ---


def test_docling_payload_carries_all_required_metadata(client):
    payload = _payload_for(client, PUBLIC_INTRO)
    for field in (
        "text",
        "source",
        "chunk_index",
        "file_type",
        "section",
        "title",
        "page_number",
        "content_type",
        "sector",
        "folders",
        "entity",
        "tags",
    ):
        assert field in payload, f"payload is missing {field}"
    assert payload["page_number"] == 1
    assert payload["content_type"] == "text"
    assert payload["title"] == "Deployment Runbook"
    assert payload["source"] == "docs/runbook.pdf"


def test_the_sector_survives_ingestion_into_the_payload(client):
    assert _payload_for(client, SECRET_TEXT)["sector"] == "restrito"
    assert _payload_for(client, PUBLIC_INTRO)["sector"] == "docs"


# --- I: retrieval must not return a restricted chunk ---


def _retrieve(client, query: str, sectors):
    settings = _settings()
    with (
        patch("docquery.retrieve.hybrid.embed_texts", side_effect=_fake_dense),
        patch(
            "docquery.retrieve.hybrid.sparse_vector",
            return_value=([4, 5, 6], [0.9, 0.8, 0.7]),
        ),
    ):
        return retrieve(query, client, settings, sectors=sectors)


def test_retrieval_hides_restricted_chunk_from_unauthorized_user(client):
    # The query is deliberately the restricted chunk's own text, so ranking
    # favours it as strongly as possible; only the filter can keep it out.
    points = _retrieve(client, SECRET_TEXT, sectors=["docs"])
    texts = {(p.payload or {}).get("text") for p in points}
    assert SECRET_TEXT not in texts
    assert PUBLIC_INTRO in texts, "public chunks should still be retrievable"


def test_retrieval_returns_restricted_chunk_for_authorized_user(client):
    points = _retrieve(client, SECRET_TEXT, sectors=["restrito"])
    texts = {(p.payload or {}).get("text") for p in points}
    assert SECRET_TEXT in texts, (
        "positive control failed: the restricted chunk is unreachable even with "
        "its sector, so the negative test above proves nothing"
    )


# --- J: expansion must not leak a restricted neighbour ---
#
# This is the subtle one. Expansion pulls neighbouring chunk_index values from
# the same source, so a public chunk sitting next to a restricted one can drag
# it into the LLM context even though retrieval correctly excluded it.


def _expand(client, sectors) -> list[dict]:
    settings = _settings(context_expansion_window=1)
    seed = [
        {
            "text": PUBLIC_INTRO,
            "source": "docs/runbook.pdf",
            "chunk_index": 0,
            "score": 1.0,
            "section": "Runbook > Overview",
            "folders": ["docs"],
        }
    ]
    return expand_contexts(seed, client, settings, sectors=sectors)


def test_expand_does_not_leak_restricted_neighbour(client):
    expanded = _expand(client, sectors=["docs"])
    merged = expanded[0]["text"]
    assert SECRET_TEXT not in merged, (
        "context expansion leaked a chunk from another compartment"
    )
    assert PUBLIC_INTRO in merged


def test_expand_includes_restricted_neighbour_for_authorized_user(client):
    expanded = _expand(client, sectors=["docs", "restrito"])
    merged = expanded[0]["text"]
    assert SECRET_TEXT in merged, (
        "positive control failed: the restricted neighbour is never returned by "
        "expansion, so the leak test above would pass even without the filter"
    )


def test_expand_filter_declares_the_sector_condition(client):
    """Guard the filter itself, not just its effect.

    Somebody removing the sector condition while keeping expansion working
    would still be caught by the leak test above, but this makes the intent
    explicit at the call site.
    """
    settings = _settings(context_expansion_window=1)
    captured = {}
    real_scroll = client.scroll

    def _spy(**kwargs):
        captured.update(kwargs)
        return real_scroll(**kwargs)

    with patch.object(client, "scroll", side_effect=_spy):
        expand_contexts(
            [
                {
                    "text": PUBLIC_INTRO,
                    "source": "docs/runbook.pdf",
                    "chunk_index": 0,
                    "score": 1.0,
                    "section": "",
                    "folders": ["docs"],
                }
            ],
            client,
            settings,
            sectors=["docs"],
        )

    conditions = captured["scroll_filter"].must
    sector_conditions = [c for c in conditions if getattr(c, "key", None) == "sector"]
    assert sector_conditions, "expansion issued a scroll with no sector filter"
    assert sector_conditions[0].match.any == ["docs"]


# --- K: documents indexed before this change stay searchable ---


def test_legacy_points_without_new_fields_remain_searchable(client):
    """Points written by the old pipeline have no title/page_number/content_type."""
    legacy = [
        Chunk(
            text="Legacy chunk indexed before the Docling change.",
            metadata={
                "source": "docs/legacy.md",
                "file_type": ".md",
                "chunk_index": 0,
                "section": "Legacy",
                "sector": "docs",
            },
        )
    ]
    settings = _settings()
    with patch("docquery.ingest.pipeline.embed_texts", side_effect=_fake_dense):
        ingest_chunks(legacy, client, settings)

    points = _retrieve(
        client, "Legacy chunk indexed before the Docling change.", ["docs"]
    )
    texts = {(p.payload or {}).get("text") for p in points}
    assert "Legacy chunk indexed before the Docling change." in texts

    expanded = expand_contexts(
        [
            {
                "text": "Legacy chunk indexed before the Docling change.",
                "source": "docs/legacy.md",
                "chunk_index": 0,
                "score": 1.0,
                "section": "Legacy",
                "folders": [],
            }
        ],
        client,
        _settings(context_expansion_window=1),
        sectors=["docs"],
    )
    assert "Legacy chunk indexed before the Docling change." in expanded[0]["text"]
