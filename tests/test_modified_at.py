"""`modified_at` answers when the document was updated, never when it was copied.

The filesystem's mtime is deliberately not a source. In a corpus that arrives by
sync, by `docker cp` or by checkout it is the date of the copy: several files
under this repo's own `docs/` share one mtime down to the sub-second, because
that is when they were written to disk, not when anyone edited them. So the date
comes from places that record an edit — the remote API of the library the file
lives in, or the metadata the editor wrote inside the file — and stays empty
when neither knows. An empty date reads as "unknown"; a copy date would read as
an answer.
"""

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    ScoredPoint,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.generate.rag import _sources_from
from docquery.ingest import pipeline, sources
from docquery.ingest.chunker import Chunk
from docquery.ingest.loader import load_document
from docquery.ingest.modified import embedded_modified_at
from docquery.retrieve.reranker import _point_to_context, rerank

FIXTURES = Path(__file__).parent / "fixtures"
COLLECTION = "test_modified_at"
DIM = 8
SP_URI = "sharepoint://contoso.sharepoint.com/sites/Docs/Documents/policies"


def _pdf_with_moddate(path: Path, moddate: str) -> Path:
    """Copy a fixture PDF, stamping /ModDate in its document information."""
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=FIXTURES / "native_text.pdf")
    writer.add_metadata({"/ModDate": moddate})
    with path.open("wb") as fh:
        writer.write(fh)
    return path


# --- embedded metadata ----------------------------------------------------


def test_pdf_moddate_is_read_as_utc(tmp_path: Path) -> None:
    pdf = _pdf_with_moddate(tmp_path / "contract.pdf", "D:20240115103000Z")
    assert embedded_modified_at(pdf) == "2024-01-15T10:30:00+00:00"


def test_pdf_moddate_with_an_offset_is_converted_to_utc(tmp_path: Path) -> None:
    """Two documents edited at the same instant must compare equal."""
    pdf = _pdf_with_moddate(tmp_path / "contract.pdf", "D:20240115103000-03'00'")
    assert embedded_modified_at(pdf) == "2024-01-15T13:30:00+00:00"


def test_pdf_without_moddate_has_no_date() -> None:
    assert embedded_modified_at(FIXTURES / "native_text.pdf") == ""


def test_docx_last_save_is_read() -> None:
    """Word writes dcterms:modified on every save, and it survives a copy."""
    assert embedded_modified_at(FIXTURES / "sample.docx") == "2013-12-23T23:15:00+00:00"


def test_ooxml_without_core_properties_has_no_date(tmp_path: Path) -> None:
    empty = tmp_path / "no-props.docx"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("word/document.xml", "<document/>")
    assert embedded_modified_at(empty) == ""


def test_markdown_has_no_embedded_date_even_with_a_fresh_mtime(tmp_path: Path) -> None:
    """The mtime says "now" and must not be mistaken for an update date."""
    md = tmp_path / "aviso.md"
    md.write_text("# aviso")
    assert md.stat().st_mtime > 0
    assert embedded_modified_at(md) == ""


def test_an_unreadable_file_has_no_date_instead_of_raising(tmp_path: Path) -> None:
    """A date is a nicety; failing the ingest over one is not."""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    assert embedded_modified_at(broken) == ""


# --- the loader stamps it -------------------------------------------------


def test_load_document_stamps_the_embedded_date(tmp_path: Path) -> None:
    pdf = _pdf_with_moddate(tmp_path / "contract.pdf", "D:20240115103000Z")
    doc = load_document(pdf)
    assert doc.metadata["modified_at"] == "2024-01-15T10:30:00+00:00"


def test_load_document_leaves_the_date_unset_when_nothing_knows_it(
    tmp_path: Path,
) -> None:
    md = tmp_path / "aviso.md"
    md.write_text("# aviso")
    assert "modified_at" not in load_document(md).metadata


# --- the remote API wins over the file ------------------------------------


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


def _sharepoint_settings() -> Settings:
    return Settings(
        openai_api_key="sk-test",
        sharepoint_tenant_id="tenant",
        sharepoint_client_id="client",
        sharepoint_client_secret="secret",
        ingest_allowed_source_prefixes=[SP_URI],
    )


def _fetch_returning(monkeypatch, downloaded: Path, modified_at: str) -> None:
    fetched = [sources.FetchedFile(downloaded, f"{SP_URI}/contract.pdf", modified_at)]
    monkeypatch.setattr(pipeline, "fetch", lambda uri, dest, settings: fetched)


def test_the_library_date_wins_over_the_embedded_one(
    captured_ingest, monkeypatch, tmp_path
) -> None:
    """SharePoint records the edit; the file records whoever exported it.

    The library is where the update happened, so its date is the fact and the
    producer's /ModDate is at best a proxy for it.
    """
    pdf = _pdf_with_moddate(tmp_path / "contract.pdf", "D:20200101000000Z")
    _fetch_returning(monkeypatch, pdf, "2024-01-15T10:30:00+00:00")

    pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())

    doc = captured_ingest["docs"][0]
    assert doc.metadata["modified_at"] == "2024-01-15T10:30:00+00:00"


def test_the_embedded_date_survives_when_the_library_has_none(
    captured_ingest, monkeypatch, tmp_path
) -> None:
    pdf = _pdf_with_moddate(tmp_path / "contract.pdf", "D:20200101000000Z")
    _fetch_returning(monkeypatch, pdf, "")

    pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())

    doc = captured_ingest["docs"][0]
    assert doc.metadata["modified_at"] == "2020-01-01T00:00:00+00:00"


# --- it reaches the chunk payload and the citation ------------------------


def _fake_dense(texts: list[str], **_kwargs) -> np.ndarray:
    """One arbitrary unit vector per chunk; retrieval quality is not the point."""
    return np.tile(np.eye(1, DIM, dtype=np.float32), (len(texts), 1))


def _in_memory_client() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    return client


def test_the_chunk_payload_carries_the_date() -> None:
    client = _in_memory_client()
    settings = Settings(openai_api_key="sk-test", qdrant_collection=COLLECTION)
    chunk = Chunk(
        text="Prazo de vigência: 24 meses.",
        metadata={
            "source": "docs/contracts/acme.pdf",
            "chunk_index": 0,
            "modified_at": "2024-01-15T10:30:00+00:00",
        },
    )
    with patch("docquery.ingest.pipeline.embed_texts", side_effect=_fake_dense):
        pipeline.ingest_chunks([chunk], client, settings)

    points, _ = client.scroll(collection_name=COLLECTION, limit=10, with_payload=True)
    assert points[0].payload["modified_at"] == "2024-01-15T10:30:00+00:00"


def test_a_document_of_unknown_date_gets_an_empty_payload_field() -> None:
    client = _in_memory_client()
    settings = Settings(openai_api_key="sk-test", qdrant_collection=COLLECTION)
    chunk = Chunk(
        text="# aviso", metadata={"source": "docs/aviso.md", "chunk_index": 0}
    )
    with patch("docquery.ingest.pipeline.embed_texts", side_effect=_fake_dense):
        pipeline.ingest_chunks([chunk], client, settings)

    points, _ = client.scroll(collection_name=COLLECTION, limit=10, with_payload=True)
    assert points[0].payload["modified_at"] == ""


def _point(modified_at: str) -> ScoredPoint:
    return ScoredPoint(
        id=1,
        version=0,
        score=0.9,
        payload={
            "text": "Prazo de vigência: 24 meses.",
            "source": "docs/contracts/acme.pdf",
            "chunk_index": 0,
            "section": "",
            "folders": ["contracts"],
            "modified_at": modified_at,
        },
    )


def test_a_retrieved_passage_carries_the_date() -> None:
    assert (
        _point_to_context(_point("2024-01-15T10:30:00+00:00"))["modified_at"]
        == "2024-01-15T10:30:00+00:00"
    )


def test_the_date_survives_the_rerank(monkeypatch) -> None:
    """The reranked path rebuilds the context dicts, so it needs the field too."""

    class _StubEncoder:
        def rank(self, query, texts, top_k=None, return_documents=False):
            return [{"corpus_id": 0, "score": 0.9}]

    monkeypatch.setattr(
        "docquery.retrieve.reranker._get_reranker", lambda _name: _StubEncoder()
    )
    contexts = rerank(
        "qual o prazo?",
        [_point("2024-01-15T10:30:00+00:00")],
        Settings(openai_api_key="sk-test", reranker_top_k=1),
    )
    assert contexts[0]["modified_at"] == "2024-01-15T10:30:00+00:00"


def test_the_citation_carries_the_date() -> None:
    contexts = [
        {
            "source": "docs/contracts/acme.pdf",
            "chunk_index": 0,
            "score": 0.9,
            "text": "Prazo de vigência: 24 meses.",
            "modified_at": "2024-01-15T10:30:00+00:00",
        }
    ]
    assert _sources_from(contexts)[0]["modified_at"] == "2024-01-15T10:30:00+00:00"


def test_a_citation_of_unknown_date_says_nothing() -> None:
    contexts = [
        {
            "source": "docs/aviso.md",
            "chunk_index": 0,
            "score": 0.9,
            "text": "# aviso",
        }
    ]
    assert _sources_from(contexts)[0]["modified_at"] == ""


def test_the_date_is_never_the_ingest_time(tmp_path: Path) -> None:
    """The regression this whole field exists to avoid.

    A file with no embedded date, ingested now, must not come out labelled with
    today — that is the ingest date wearing the update date's name.
    """
    md = tmp_path / "aviso.md"
    md.write_text("# aviso")
    doc = load_document(md)
    today = datetime.now(UTC).date().isoformat()
    assert today not in str(doc.metadata.get("modified_at", ""))
