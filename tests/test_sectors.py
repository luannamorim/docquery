"""Tests for sector compartments: the access boundary imposed at retrieval.

Compartments are what a numeric clearance level could not express — RH reads
RH and Financeiro reads Financeiro, neither containing the other. The mutual
exclusion below is the property the whole design exists for.

In-memory Qdrant (QdrantClient(":memory:")). Every point
shares one sparse vector on purpose, so all four are always candidates and the
filter is the only thing that can remove one: a source missing from a result is
evidence the compartment excluded it, not that the query missed it.
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
from docquery.retrieve.expand import expand_contexts
from docquery.retrieve.hybrid import retrieve

COLLECTION = "test_sectors"
DIM = 8

RH = "rh/ferias.md"
FIN = "financeiro/notas.md"
FIN_NESTED = "financeiro/rh/folha.md"
ROOT = "aviso.md"
EVERYTHING = {RH, FIN, FIN_NESTED, ROOT}


def _settings(**overrides) -> Settings:
    defaults = {
        "qdrant_collection": COLLECTION,
        "embedding_dimension": DIM,
        "retrieval_top_k": 10,
        "openai_api_key": "sk-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _point(source: str, sector: str, **payload_extra) -> PointStruct:
    return PointStruct(
        id=int(hashlib.sha256(source.encode()).hexdigest()[:16], 16),
        vector={
            "dense": [1.0] + [0.0] * (DIM - 1),
            "sparse": SparseVector(indices=[1, 2, 3], values=[0.5, 0.3, 0.2]),
        },
        payload={
            "text": f"conteudo de {source}",
            "source": source,
            "chunk_index": 0,
            "file_type": ".md",
            "section": "",
            "sector": sector,
            "folders": [sector] if sector else [],
            "entity": "",
            "tags": [],
            **payload_extra,
        },
    )


@pytest.fixture()
def client() -> QdrantClient:
    c = QdrantClient(":memory:")
    c.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    c.upsert(
        collection_name=COLLECTION,
        points=[
            _point(RH, "rh"),
            _point(FIN, "financeiro"),
            # Findable under "rh" by the folder facet, but financeiro owns it.
            _point(FIN_NESTED, "financeiro", folders=["financeiro", "rh"]),
            # At the ingest root: belongs to no compartment.
            _point(ROOT, ""),
        ],
    )
    return c


def _retrieve(client, **kwargs) -> set[str]:
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array([[1.0] + [0.0] * (DIM - 1)])
        mock_sparse.return_value = ([1, 2, 3], [0.5, 0.3, 0.2])
        points = retrieve("consulta", client, _settings(), **kwargs)
    return {(p.payload or {}).get("source", "") for p in points}


# --- the property levels could not express --------------------------------


def test_rh_does_not_reach_financeiro(client):
    assert _retrieve(client, sectors=["rh"]) == {RH}


def test_financeiro_does_not_reach_rh(client):
    """The other direction — neither compartment contains the other."""
    assert _retrieve(client, sectors=["financeiro"]) == {FIN, FIN_NESTED}


def test_several_sectors_reach_the_union(client):
    assert _retrieve(client, sectors=["rh", "financeiro"]) == {RH, FIN, FIN_NESTED}


# --- fail-closed ----------------------------------------------------------


def test_no_sectors_returns_nothing(client):
    """A token with no mapped role reads nothing at all."""
    assert _retrieve(client, sectors=[]) == set()


def test_no_sectors_never_reaches_qdrant():
    """The short-circuit is the guarantee: an empty MatchAny is not relied on."""

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"Qdrant was queried ({name}) with no sectors")

    assert retrieve("consulta", Exploding(), _settings(), sectors=[]) == []


def test_none_means_no_filter(client):
    """Auth off: there is no identity to enforce, so nothing is hidden."""
    assert _retrieve(client, sectors=None) == EVERYTHING


def test_a_document_without_a_sector_is_unreachable(client):
    """No role can name "", so a file at the ingest root is closed off."""
    assert ROOT not in _retrieve(client, sectors=["rh"])
    assert _retrieve(client, sectors=[""]) == set()


# --- the compartment is not the folder facet ------------------------------


def test_a_nested_folder_does_not_grant_another_sector(client):
    """financeiro/rh/folha.md carries "rh" in folders but belongs to financeiro.

    Reusing `folders` for access control would hand this document to RH, because
    that facet matches at any depth. The compartment reads the top of the path.
    """
    assert FIN_NESTED not in _retrieve(client, sectors=["rh"])
    assert FIN_NESTED in _retrieve(client, sectors=["financeiro"])
    # And the facet does still find it, for whoever is allowed to see it.
    assert _retrieve(client, sectors=["financeiro"], folders=["rh"]) == {FIN_NESTED}


def test_a_folder_filter_cannot_widen_the_compartment(client):
    """The caller chooses folders; the server imposes the sector."""
    assert _retrieve(client, sectors=["rh"], folders=["financeiro"]) == set()


# --- expansion ------------------------------------------------------------


def test_expansion_does_not_leak_across_compartments(client):
    """The step that fetches chunks nobody searched for repeats the filter."""
    seed = [
        {
            "text": f"conteudo de {FIN}",
            "source": FIN,
            "chunk_index": 0,
            "score": 1.0,
            "section": "",
        }
    ]
    expanded = expand_contexts(
        seed, client, _settings(context_expansion_window=1), sectors=["rh"]
    )
    assert all(not ctx["text"] for ctx in expanded)


def test_expansion_keeps_what_the_compartment_allows(client):
    """Control: the same call succeeds for the sector that owns the document."""
    seed = [
        {
            "text": f"conteudo de {FIN}",
            "source": FIN,
            "chunk_index": 0,
            "score": 1.0,
            "section": "",
        }
    ]
    expanded = expand_contexts(
        seed, client, _settings(context_expansion_window=1), sectors=["financeiro"]
    )
    assert expanded[0]["text"] == f"conteudo de {FIN}"


def test_expansion_with_no_sectors_returns_nothing(client):
    seed = [
        {
            "text": "x",
            "source": RH,
            "chunk_index": 0,
            "score": 1.0,
            "section": "",
        }
    ]
    assert (
        expand_contexts(seed, client, _settings(context_expansion_window=1), sectors=[])
        == []
    )


# --- the demo header ------------------------------------------------------


def _capturing_pipeline() -> tuple[dict, callable]:
    captured: dict = {}

    def _pipeline(query: str, settings=None, **kwargs) -> dict:
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

    return captured, _pipeline


def _post(json_body, headers=None) -> dict:
    from fastapi.testclient import TestClient

    from docquery.api.app import app

    captured, pipeline = _capturing_pipeline()
    with patch("docquery.api.routes.query_pipeline", side_effect=pipeline):
        response = TestClient(app).post("/query", json=json_body, headers=headers or {})
    assert response.status_code == 200
    return captured


def test_header_restricts_the_query():
    captured = _post({"query": "x"}, {"X-User-Sectors": "rh,juridico"})
    assert captured["sectors"] == ["juridico", "rh"]


def test_header_is_normalized_like_folder_names():
    captured = _post({"query": "x"}, {"X-User-Sectors": " RH , rh "})
    assert captured["sectors"] == ["rh"]


def test_without_the_header_nothing_is_restricted():
    """Auth off and no header: the pipeline is told not to filter at all."""
    assert _post({"query": "x"})["sectors"] is None


def test_a_blank_header_reads_nothing():
    """Explicitly asking for no sector is not the same as not asking."""
    assert _post({"query": "x"}, {"X-User-Sectors": " , "})["sectors"] == []


def test_an_unreachable_sector_does_not_claim_the_index_is_empty(client, monkeypatch):
    """Telling the caller to ingest would send them re-indexing what is there."""
    from docquery.generate import rag

    monkeypatch.setattr(rag, "QdrantClient", lambda **kwargs: client)
    monkeypatch.setattr(rag, "OpenAI", lambda **kwargs: None)
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array([[1.0] + [0.0] * (DIM - 1)])
        mock_sparse.return_value = ([1, 2, 3], [0.5, 0.3, 0.2])
        result = rag.query_pipeline("consulta", _settings(), sectors=["inexistente"])

    assert result["sources"] == []
    assert "ingest" not in result["answer"].lower()
    assert "access" in result["answer"].lower()
