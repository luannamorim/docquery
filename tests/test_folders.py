"""Tests for folder facets: server-side derivation and query-time filtering.

The corpus structure is the taxonomy — a folder is a facet the moment it is
ingested, with nothing to configure. These tests cover the derivation itself,
both ingest entry points (local tree and remote URI), and the query filter,
which matches a folder name at any depth and is ANDed with the sector filter.

Query-side tests mirror test_sectors.py's in-memory Qdrant approach
(QdrantClient(":memory:")), so no Docker is needed.
"""

import hashlib
import unicodedata
from pathlib import Path
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
from docquery.folders import folder_segments, normalize_segment, sector_of
from docquery.ingest import pipeline, sources
from docquery.retrieve.hybrid import retrieve

SP_URI = "sharepoint://contoso.sharepoint.com/sites/Corp/Documentos"
COLLECTION = "test_folders"
DIM = 8


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test"}
    defaults.update(overrides)
    return Settings(**defaults)


def _sharepoint_settings(**overrides) -> Settings:
    defaults = {
        "sharepoint_tenant_id": "tenant",
        "sharepoint_client_id": "client",
        "sharepoint_client_secret": "secret",
    }
    defaults.update(overrides)
    return _settings(**defaults)


# --- derivation -----------------------------------------------------------


def test_folder_segments_drops_the_file_name():
    assert folder_segments("rh/ferias/politica.md") == ["rh", "ferias"]


def test_file_at_the_root_has_no_folders():
    assert folder_segments("politica.md") == []


def test_segments_are_lowercased():
    """Folder casing is a display choice in SharePoint, not an identity."""
    assert folder_segments("RH/Beneficios/a.md") == ["rh", "beneficios"]


def test_backslash_paths_are_normalized():
    assert folder_segments("rh\\ferias\\a.md") == ["rh", "ferias"]


def test_empty_segments_are_dropped():
    assert folder_segments("rh//ferias/ /a.md") == ["rh", "ferias"]


def test_decomposed_and_composed_accents_agree():
    """A macOS (NFD) path must face the same segment a caller types (NFC)."""
    decomposed = unicodedata.normalize("NFD", "Férias")
    assert folder_segments(f"{decomposed}/a.md") == [normalize_segment("Férias")]
    assert folder_segments(f"{decomposed}/a.md") == ["férias"]


def test_spaces_and_accents_survive_normalization():
    """Callers filter by the folder name they see, not by a slug of it."""
    assert folder_segments("Recursos Humanos/a.md") == ["recursos humanos"]


# --- sector (access compartment) ------------------------------------------


def test_sector_is_the_top_level_folder():
    assert sector_of(folder_segments("rh/beneficios/plano.pdf")) == "rh"


def test_sector_ignores_nested_segments():
    """The security-critical case: a folder named after another sector.

    `folders` matches at any depth, so this document is findable under "rh" —
    but it belongs to financeiro, and only financeiro may read it.
    """
    segments = folder_segments("financeiro/rh/nota.pdf")
    assert segments == ["financeiro", "rh"]
    assert sector_of(segments) == "financeiro"


def test_a_file_at_the_root_has_no_sector():
    assert sector_of(folder_segments("aviso.md")) == ""


# --- local ingest ---------------------------------------------------------


@pytest.fixture
def captured_ingest(monkeypatch):
    """Stub out Qdrant so ingest can be observed without a server."""
    captured: dict = {}

    def _ingest_documents(docs, client, settings, current_sources, orphan_prefix):
        captured["docs"] = docs
        return {"chunks": len(docs), "deleted": 0}

    monkeypatch.setattr(pipeline, "_qdrant_client", lambda settings: object())
    monkeypatch.setattr(pipeline, "ensure_collection", lambda client, settings: None)
    monkeypatch.setattr(pipeline, "_ingest_documents", _ingest_documents)
    return captured


def _folders_by_source(captured: dict) -> dict[str, list[str]]:
    return {
        Path(str(doc.metadata["source"])).name: doc.metadata["folders"]
        for doc in captured["docs"]
    }


def test_local_folders_are_relative_to_the_ingested_root(captured_ingest, tmp_path):
    (tmp_path / "rh").mkdir()
    (tmp_path / "rh" / "ferias.md").write_text("# ferias")
    (tmp_path / "financeiro" / "2024").mkdir(parents=True)
    (tmp_path / "financeiro" / "2024" / "notas.md").write_text("# notas")
    (tmp_path / "raiz.md").write_text("# raiz")

    pipeline.ingest_path(tmp_path, settings=_settings())

    assert _folders_by_source(captured_ingest) == {
        "ferias.md": ["rh"],
        "notas.md": ["financeiro", "2024"],
        "raiz.md": [],
    }


def test_local_ingest_labels_each_document_with_its_sector(captured_ingest, tmp_path):
    (tmp_path / "rh" / "beneficios").mkdir(parents=True)
    (tmp_path / "rh" / "beneficios" / "plano.md").write_text("# plano")
    (tmp_path / "financeiro").mkdir()
    (tmp_path / "financeiro" / "notas.md").write_text("# notas")
    (tmp_path / "raiz.md").write_text("# raiz")

    pipeline.ingest_path(tmp_path, settings=_settings())

    by_name = {
        Path(str(d.metadata["source"])).name: d.metadata["sector"]
        for d in captured_ingest["docs"]
    }
    assert by_name == {"plano.md": "rh", "notas.md": "financeiro", "raiz.md": ""}


def test_ingesting_a_single_file_yields_no_folders(captured_ingest, tmp_path):
    """A lone file is its own root — there is no tree to derive facets from."""
    target = tmp_path / "rh" / "ferias.md"
    target.parent.mkdir()
    target.write_text("# ferias")

    pipeline.ingest_path(target, settings=_settings())

    assert captured_ingest["docs"][0].metadata["folders"] == []


# --- remote ingest --------------------------------------------------------


def _fetch_returning(monkeypatch, tmp_path, *source_uris: str) -> None:
    downloaded = tmp_path / "scratch.md"
    downloaded.write_text("# hello")
    fetched = [sources.FetchedFile(downloaded, uri) for uri in source_uris]
    monkeypatch.setattr(pipeline, "fetch", lambda uri, dest, settings: fetched)


def test_remote_folders_come_from_the_uri(captured_ingest, monkeypatch, tmp_path):
    _fetch_returning(monkeypatch, tmp_path, f"{SP_URI}/RH/Beneficios/plano.md")

    pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())

    assert captured_ingest["docs"][0].metadata["folders"] == ["rh", "beneficios"]


def test_remote_ingest_labels_the_sector_from_the_uri(
    captured_ingest, monkeypatch, tmp_path
):
    _fetch_returning(monkeypatch, tmp_path, f"{SP_URI}/RH/Beneficios/plano.md")

    pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())

    assert captured_ingest["docs"][0].metadata["sector"] == "rh"


def test_remote_file_at_the_library_root_has_no_folders(
    captured_ingest, monkeypatch, tmp_path
):
    _fetch_returning(monkeypatch, tmp_path, f"{SP_URI}/aviso.md")

    pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())

    assert captured_ingest["docs"][0].metadata["folders"] == []


def test_trailing_slash_on_the_source_uri_does_not_shift_segments(
    captured_ingest, monkeypatch, tmp_path
):
    """Fetchers strip it when building source_uri; derivation must too."""
    _fetch_returning(monkeypatch, tmp_path, f"{SP_URI}/RH/plano.md")

    pipeline.ingest_source(f"{SP_URI}/", settings=_sharepoint_settings())

    assert captured_ingest["docs"][0].metadata["folders"] == ["rh"]


# --- query filter ---------------------------------------------------------


def _query_settings(**overrides) -> Settings:
    defaults = {
        "qdrant_collection": COLLECTION,
        "embedding_dimension": DIM,
        "retrieval_top_k": 10,
    }
    defaults.update(overrides)
    return _settings(**defaults)


def _point(source: str, payload_extra: dict) -> PointStruct:
    return PointStruct(
        id=int(hashlib.sha256(source.encode()).hexdigest()[:16], 16),
        vector={
            "dense": [1.0] + [0.0] * (DIM - 1),
            "sparse": SparseVector(indices=[1, 2, 3], values=[0.5, 0.3, 0.2]),
        },
        payload={
            "text": "payment terms",
            "source": source,
            "chunk_index": 0,
            "file_type": ".md",
            "section": "",
            "entity": "",
            "tags": [],
            **payload_extra,
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
            _point("rh/ferias.md", {"folders": ["rh"], "tags": ["ferias"]}),
            _point("rh/2024/plano.md", {"folders": ["rh", "2024"]}),
            _point("financeiro/notas.md", {"folders": ["financeiro"]}),
            _point("aviso.md", {"folders": []}),
            # Ingested before folder facets existed: the field is simply absent.
            _point("legado/antigo.md", {}),
        ],
    )
    return client


def _retrieve(client, settings=None, **kwargs) -> set[str]:
    with (
        patch("docquery.retrieve.hybrid.embed_texts") as mock_embed,
        patch("docquery.retrieve.hybrid.sparse_vector") as mock_sparse,
    ):
        mock_embed.return_value = np.array([[1.0] + [0.0] * (DIM - 1)])
        mock_sparse.return_value = ([1, 2, 3], [0.5, 0.3, 0.2])
        points = retrieve(
            "payment terms", client, settings or _query_settings(), **kwargs
        )
    return {(p.payload or {}).get("source", "") for p in points}


def test_no_filter_returns_every_folder(qdrant_client):
    assert _retrieve(qdrant_client) == {
        "rh/ferias.md",
        "rh/2024/plano.md",
        "financeiro/notas.md",
        "aviso.md",
        "legado/antigo.md",
    }


def test_folder_filter_restricts_to_that_folder(qdrant_client):
    sources = _retrieve(qdrant_client, folders=["rh"])
    assert sources == {"rh/ferias.md", "rh/2024/plano.md"}


def test_folder_filter_matches_at_any_depth(qdrant_client):
    """A nested folder is a facet in its own right, not only via its parent."""
    assert _retrieve(qdrant_client, folders=["2024"]) == {"rh/2024/plano.md"}


def test_folder_filter_is_case_insensitive(qdrant_client):
    assert _retrieve(qdrant_client, folders=["RH"]) == {
        "rh/ferias.md",
        "rh/2024/plano.md",
    }


def test_several_folders_are_ored_together(qdrant_client):
    assert _retrieve(qdrant_client, folders=["financeiro", "2024"]) == {
        "financeiro/notas.md",
        "rh/2024/plano.md",
    }


def test_folder_filter_is_anded_with_other_filters(qdrant_client):
    assert _retrieve(qdrant_client, folders=["rh"], tags=["ferias"]) == {"rh/ferias.md"}


def test_blank_folder_names_are_treated_as_no_filter(qdrant_client):
    """Consistent with tags: an empty selection must not silently match nothing."""
    assert len(_retrieve(qdrant_client, folders=["  "])) == 5


def test_chunks_without_the_field_are_excluded_when_filtering(qdrant_client):
    """Points ingested before this feature carry no folders; re-ingest restores them."""
    assert "legado/antigo.md" not in _retrieve(qdrant_client, folders=["legado"])


def test_api_propagates_the_folders_filter():
    """The /query endpoint forwards folders to the pipeline."""
    from fastapi.testclient import TestClient

    from docquery.api.app import app

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

    with patch("docquery.api.routes.query_pipeline", side_effect=_pipeline):
        client = TestClient(app)
        response = client.post(
            "/query", json={"query": "prazo de ferias", "folders": ["rh"]}
        )

    assert response.status_code == 200
    assert captured.get("folders") == ["rh"]
