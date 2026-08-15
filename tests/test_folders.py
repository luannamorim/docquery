"""Tests for folder facets: server-side derivation from the ingested tree.

The corpus structure is the taxonomy — a folder is a facet the moment it is
ingested, with nothing to configure. These tests cover the derivation itself
and both ingest entry points (local tree and remote URI).
"""

import unicodedata
from pathlib import Path

import pytest

from docquery.config import Settings
from docquery.folders import folder_segments, normalize_segment
from docquery.ingest import pipeline, sources

SP_URI = "sharepoint://contoso.sharepoint.com/sites/Corp/Documentos"


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
