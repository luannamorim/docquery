"""End-to-end Docling conversion over the committed fixtures.

These run real layout/table/OCR inference, so they need Docling's models and
take minutes rather than seconds. They are opt-in: CI runs the fast unit tests
in test_docling_loader.py instead.

Enable with:
    DOCQUERY_DOCLING_INTEGRATION=1 uv run pytest -m docling
"""

import os
from pathlib import Path

import pytest

from docquery.config import Settings
from docquery.ingest.chunker import chunk_document
from docquery.ingest.loader import load_document

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = [
    pytest.mark.docling,
    pytest.mark.skipif(
        os.environ.get("DOCQUERY_DOCLING_INTEGRATION") != "1",
        reason="set DOCQUERY_DOCLING_INTEGRATION=1 to run Docling conversion tests",
    ),
]


def _settings(**overrides) -> Settings:
    defaults = {"docling_enabled": True}
    artifacts = os.environ.get("DOCLING_ARTIFACTS_PATH")
    if artifacts:
        defaults["docling_artifacts_path"] = Path(artifacts)
    defaults.update(overrides)
    return Settings(**defaults)


def _convert(name: str, **overrides):
    settings = _settings(**overrides)
    doc = load_document(FIXTURES / name, settings)
    return doc, chunk_document(doc, settings)


# --- A: PDF with native text ---


def test_native_text_pdf_is_extracted_and_chunked():
    doc, chunks = _convert("native_text.pdf", docling_table_structure=False)
    assert doc.dl_doc is not None, "the file did not go through Docling"
    assert "RBK-7781-ZULU" in doc.content
    assert chunks
    assert any("rollback token" in c.text.lower() for c in chunks)


# --- B: scanned PDF (OCR) ---


def test_scanned_pdf_produces_text_through_ocr():
    doc, chunks = _convert(
        "scanned.pdf", docling_ocr_enabled=True, docling_table_structure=False
    )
    text = doc.content.lower()
    assert text.strip(), "OCR produced no text at all for the scanned page"
    assert "scanned" in text or "maintenance" in text or "generator" in text
    assert chunks


def test_scanned_pdf_yields_no_text_when_ocr_is_disabled():
    """Documents the cost/benefit of the flag: no OCR means no text here."""
    doc, _ = _convert(
        "scanned.pdf", docling_ocr_enabled=False, docling_table_structure=False
    )
    assert "generator" not in doc.content.lower()


# --- C: PDF with a table ---


def test_table_pdf_keeps_the_table_coherent():
    doc, chunks = _convert("table.pdf", docling_table_structure=True)
    table_chunks = [c for c in chunks if c.metadata["content_type"] == "table"]
    assert table_chunks, "no chunk was classified as a table"

    # Querying one cell must return a fragment that still reads as a table.
    matching = [c for c in table_chunks if "101275" in c.text]
    assert matching, "the queried cell is not retrievable from any table chunk"
    chunk = matching[0]
    assert "South" in chunk.text, "the cell lost its own row"
    assert "Revenue" in chunk.text, "the fragment lost the header row"


# --- D: multi-page PDF ---


def test_multipage_pdf_reports_the_correct_page_per_chunk():
    _, chunks = _convert("multipage.pdf", docling_table_structure=False)
    sentinels = {
        "ALPHA-PAGE-ONE": 1,
        "BRAVO-PAGE-TWO": 2,
        "CHARLIE-PAGE-THREE": 3,
    }
    for sentinel, expected_page in sentinels.items():
        owning = [c for c in chunks if sentinel in c.text]
        assert owning, f"{sentinel} was not found in any chunk"
        for chunk in owning:
            assert chunk.metadata["page_number"] == expected_page, (
                f"{sentinel} belongs to page {expected_page} but its chunk "
                f"reports page {chunk.metadata['page_number']}"
            )


# --- E: PNG image ---


def test_png_image_is_converted_without_error():
    doc, chunks = _convert(
        "image.png", docling_ocr_enabled=True, docling_table_structure=False
    )
    assert doc.dl_doc is not None
    assert doc.metadata["file_type"] == ".png"
    # The placard text is recovered by OCR; assert loosely because OCR output
    # varies by engine version.
    assert "safety" in doc.content.lower() or "protection" in doc.content.lower()
    assert chunks


# --- F: DOCX ---


def test_docx_text_and_structure_are_extracted():
    doc, chunks = _convert("sample.docx")
    assert doc.dl_doc is not None
    assert "thirty days" in doc.content
    assert chunks

    sections = {c.metadata["section"] for c in chunks}
    assert any("Payment Terms" in s for s in sections), (
        f"heading structure was lost, sections were {sections}"
    )
    # DOCX has no pagination, so provenance is reported as "no page".
    assert all(c.metadata["page_number"] == 0 for c in chunks)


def test_docx_table_is_recovered():
    _, chunks = _convert("sample.docx")
    table_chunks = [c for c in chunks if c.metadata["content_type"] == "table"]
    assert table_chunks
    assert any("42500" in c.text for c in table_chunks)
