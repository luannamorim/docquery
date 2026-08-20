"""Unit tests for the Docling parsing layer.

These build DoclingDocument objects directly instead of converting real files,
so they exercise the wiring, metadata mapping and failure handling without
needing Docling's layout/OCR models. End-to-end conversion of real documents
lives in test_docling_integration.py.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
)

from docquery.config import Settings
from docquery.ingest import docling_loader
from docquery.ingest.chunker import chunk_document
from docquery.ingest.loader import Document, iter_ingestable_files, load_document

FIXTURES = Path(__file__).parent / "fixtures"

PARAGRAPH = (
    "The platform ingests documents from the corpus root and writes the "
    "resulting chunks to the vector store. Operators review the dashboards "
    "after every release and record anomalies in the shared incident log "
    "before closing the change request. Routine maintenance happens inside "
    "the weekly window agreed with the platform group, while emergency work "
    "follows the documented on-call escalation path instead."
)


def _prov(page: int) -> ProvenanceItem:
    return ProvenanceItem(
        page_no=page,
        bbox=BoundingBox(l=0, t=100, r=200, b=80, coord_origin=CoordOrigin.BOTTOMLEFT),
        charspan=(0, 10),
    )


def _doc_with_pages() -> DoclingDocument:
    """A three-page document, one distinct heading and body paragraph per page."""
    doc = DoclingDocument(name="synthetic")
    for page in (1, 2, 3):
        doc.add_page(page_no=page, size=Size(width=612, height=792))
    doc.add_text(label=DocItemLabel.TITLE, text="Operations Manual", prov=_prov(1))
    for page, heading in enumerate(["Ingestion", "Retrieval", "Evaluation"], start=1):
        doc.add_heading(text=heading, level=1, prov=_prov(page))
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=f"{PARAGRAPH} The marker for this page is SENTINEL-{page}.",
            prov=_prov(page),
        )
    return doc


def _doc_with_table() -> DoclingDocument:
    """A document whose only content is a header row plus four data rows."""
    doc = DoclingDocument(name="synthetic-table")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_heading(text="Quarterly Revenue", level=1, prov=_prov(1))
    rows = [
        ("Region", "Quarter", "Revenue"),
        ("North", "Q1", "118400"),
        ("North", "Q2", "126950"),
        ("South", "Q1", "94300"),
        ("South", "Q2", "101275"),
    ]
    table = doc.add_table(data=TableData(num_rows=len(rows), num_cols=3), prov=_prov(1))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            doc.add_table_cell(
                table_item=table,
                cell=TableCell(
                    start_row_offset_idx=r,
                    end_row_offset_idx=r + 1,
                    start_col_offset_idx=c,
                    end_col_offset_idx=c + 1,
                    text=cell,
                    column_header=(r == 0),
                ),
            )
    return doc


def _as_document(dl_doc: DoclingDocument, **metadata) -> Document:
    meta = {"source": "docs/manual.pdf", "file_type": ".pdf"}
    meta.update(metadata)
    return Document(content=dl_doc.export_to_markdown(), metadata=meta, dl_doc=dl_doc)


# --- feature flag: the legacy path must stay untouched (invariant I6) ---


def test_disabled_flag_keeps_pdf_on_legacy_parser(tmp_path):
    pdf = FIXTURES / "native_text.pdf"
    doc = load_document(pdf, Settings(docling_enabled=False))
    assert doc.dl_doc is None
    assert doc.metadata["file_type"] == ".pdf"
    assert "pages" in doc.metadata


def test_disabled_flag_does_not_list_docling_only_extensions(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.docx").write_bytes(b"not really a docx")
    found = iter_ingestable_files(tmp_path, Settings(docling_enabled=False))
    assert [p.name for p in found] == ["a.txt"]


def test_enabled_flag_lists_docling_extensions(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.docx").write_bytes(b"not really a docx")
    found = iter_ingestable_files(tmp_path, Settings(docling_enabled=True))
    assert [p.name for p in found] == ["a.txt", "b.docx"]


def test_text_and_markdown_never_route_to_docling():
    settings = Settings(docling_enabled=True)
    assert not docling_loader.handles(Path("a.txt"), settings)
    assert not docling_loader.handles(Path("a.md"), settings)
    assert docling_loader.handles(Path("a.pdf"), settings)
    assert docling_loader.handles(Path("a.DOCX"), settings)


def test_markdown_keeps_frontmatter_handling_with_docling_enabled(tmp_path):
    md = tmp_path / "note.md"
    md.write_text("---\ntitle: Quarterly\nsector: dir\n---\n\n# Body\n\ntext here")
    doc = load_document(md, Settings(docling_enabled=True))
    assert doc.dl_doc is None
    assert doc.metadata["title"] == "Quarterly"
    # An author-supplied sector is still refused; it is derived server-side.
    assert "sector" not in doc.metadata


# --- metadata mapping ---


def test_chunks_carry_page_number_section_and_content_type():
    chunks = chunk_document(
        _as_document(_doc_with_pages()), Settings(docling_enabled=True)
    )
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.metadata["page_number"] >= 1
        assert chunk.metadata["content_type"] in {"text", "table", "figure"}
        assert chunk.metadata["section"]

    # Each page's sentinel must be reported on the page it actually came from.
    for page in (1, 2, 3):
        owning = [c for c in chunks if f"SENTINEL-{page}" in c.text]
        assert owning, f"no chunk contains SENTINEL-{page}"
        assert all(c.metadata["page_number"] == page for c in owning)


def test_chunk_index_is_sequential():
    chunks = chunk_document(
        _as_document(_doc_with_pages()), Settings(docling_enabled=True)
    )
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_section_uses_heading_breadcrumb():
    chunks = chunk_document(
        _as_document(_doc_with_pages()), Settings(docling_enabled=True)
    )
    sections = {c.metadata["section"] for c in chunks}
    assert "Operations Manual > Ingestion" in sections


def test_page_number_is_zero_without_provenance():
    doc = DoclingDocument(name="no-pages")
    doc.add_heading(text="Terms", level=1)
    doc.add_text(label=DocItemLabel.TEXT, text=PARAGRAPH)
    chunks = chunk_document(
        _as_document(doc, file_type=".docx"), Settings(docling_enabled=True)
    )
    assert chunks
    assert all(c.metadata["page_number"] == 0 for c in chunks)


# --- tables ---


def test_table_chunk_is_marked_and_kept_coherent():
    chunks = chunk_document(
        _as_document(_doc_with_table()), Settings(docling_enabled=True)
    )
    table_chunks = [c for c in chunks if c.metadata["content_type"] == "table"]
    assert table_chunks, "table content was not classified as a table chunk"

    # A query about one cell must land on a chunk that still reads as a table:
    # the row is intact and the header row is present alongside it.
    matching = [c for c in table_chunks if "101275" in c.text]
    assert matching, "no table chunk contains the queried cell"
    chunk = matching[0]
    assert "South" in chunk.text and "Q2" in chunk.text
    assert "Revenue" in chunk.text, "header row missing from the table fragment"
    assert "|" in chunk.text, "table lost its markdown grid"


def test_large_table_fragments_repeat_the_header():
    """A table too large for one chunk keeps its header on every fragment."""
    doc = DoclingDocument(name="big-table")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    n_rows = 120
    table = doc.add_table(data=TableData(num_rows=n_rows, num_cols=3), prov=_prov(1))
    header = ("Region", "Quarter", "Revenue")
    for r in range(n_rows):
        values = header if r == 0 else (f"Region{r}", f"Q{r % 4 + 1}", str(1000 + r))
        for c, cell in enumerate(values):
            doc.add_table_cell(
                table_item=table,
                cell=TableCell(
                    start_row_offset_idx=r,
                    end_row_offset_idx=r + 1,
                    start_col_offset_idx=c,
                    end_col_offset_idx=c + 1,
                    text=cell,
                    column_header=(r == 0),
                ),
            )
    chunks = chunk_document(_as_document(doc), Settings(docling_enabled=True))
    fragments = [c for c in chunks if c.metadata["content_type"] == "table"]
    assert len(fragments) > 1, "table was expected to span several chunks"
    for fragment in fragments:
        assert "Revenue" in fragment.text, "a table fragment lost the header row"


# --- access metadata (invariant I1: it survives the new parser) ---


def test_access_metadata_propagates_to_docling_chunks():
    chunks = chunk_document(
        _as_document(_doc_with_pages(), sector="policies", folders=["policies"]),
        Settings(docling_enabled=True),
    )
    assert chunks
    assert all(c.metadata["sector"] == "policies" for c in chunks)
    assert all(c.metadata["folders"] == ["policies"] for c in chunks)


# --- failure handling ---


def test_pdf_failure_falls_back_to_legacy_parser():
    settings = Settings(docling_enabled=True)
    with patch.object(
        docling_loader, "load_with_docling", side_effect=RuntimeError("boom")
    ):
        doc = load_document(FIXTURES / "native_text.pdf", settings)
    assert doc.dl_doc is None, "expected the legacy pypdf result"
    assert doc.content.strip()


def test_docling_only_format_failure_raises_conversion_error(tmp_path):
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a real docx")
    with pytest.raises(docling_loader.DoclingConversionError):
        load_document(broken, Settings(docling_enabled=True))


def test_directory_ingest_skips_broken_docling_only_file(tmp_path):
    (tmp_path / "good.txt").write_text("readable content")
    (tmp_path / "broken.docx").write_bytes(b"not a real docx")
    from docquery.ingest.loader import load_directory

    docs = load_directory(tmp_path, Settings(docling_enabled=True))
    assert [d.metadata["source"] for d in docs] == [str(tmp_path / "good.txt")]


def test_directory_ingest_still_aborts_on_legacy_parser_failure(tmp_path):
    # Legacy behaviour is unchanged: an unreadable .txt fails the whole run.
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00invalid utf8 \xc3\x28")
    with pytest.raises(UnicodeDecodeError):
        from docquery.ingest.loader import load_directory

        load_directory(tmp_path, Settings(docling_enabled=True))


def test_file_larger_than_limit_is_rejected_with_a_clear_error(tmp_path):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024))
    settings = Settings(docling_enabled=True, docling_max_file_mb=1)
    with pytest.raises(docling_loader.DoclingLimitExceeded, match="MB limit"):
        docling_loader.convert(big, settings)


def test_pdf_over_the_page_limit_is_rejected_before_conversion(tmp_path):
    settings = Settings(docling_enabled=True, docling_max_pages=2)
    # multipage.pdf has three pages; the limit must be enforced without the
    # converter (and its models) ever being touched.
    with patch.object(docling_loader, "_converter") as converter:
        with pytest.raises(docling_loader.DoclingLimitExceeded, match="page limit"):
            load_document(FIXTURES / "multipage.pdf", settings)
    converter.assert_not_called()


def test_pdf_within_the_page_limit_passes_the_check():
    settings = Settings(docling_enabled=True, docling_max_pages=10)
    # Reaches the converter rather than being rejected by the page check.
    with patch.object(
        docling_loader, "_converter", side_effect=RuntimeError("reached converter")
    ):
        doc = load_document(FIXTURES / "multipage.pdf", settings)
    assert doc.dl_doc is None, "expected the pypdf fallback after the stub failed"


def test_oversized_pdf_is_not_silently_downgraded_to_the_legacy_parser(tmp_path):
    """A size limit is a guard, not a hint: it must not fall back to pypdf."""
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024))
    settings = Settings(docling_enabled=True, docling_max_file_mb=1)
    with pytest.raises(docling_loader.DoclingLimitExceeded):
        load_document(big, settings)


def _partial_result(category):
    """A ConversionResult stand-in: partial status, one structured error."""
    from types import SimpleNamespace

    from docling.datamodel.base_models import (
        ConversionStatus,
        DoclingComponentType,
        ErrorItem,
    )

    return SimpleNamespace(
        status=ConversionStatus.PARTIAL_SUCCESS,
        errors=[
            ErrorItem(
                component_type=DoclingComponentType.PIPELINE,
                module_name="base_pipeline",
                error_message="Document processing timeout: exceeded 300.000s limit",
                category=category,
            )
        ],
        document=_doc_with_pages(),
    )


def _stub_converter(result):
    class _Converter:
        def convert(self, path, **kwargs):
            return result

    return _Converter()


def test_timed_out_conversion_is_rejected_not_indexed_partially():
    """A timeout mid-OCR must not silently index the pre-OCR half of the file."""
    from docling.datamodel.base_models import FailureCategory

    settings = Settings(docling_enabled=True)
    result = _partial_result(FailureCategory.TIMEOUT)
    with patch.object(
        docling_loader, "_converter", return_value=_stub_converter(result)
    ):
        with pytest.raises(
            docling_loader.DoclingLimitExceeded, match="DOCLING_TIMEOUT_SECONDS"
        ):
            docling_loader.convert(FIXTURES / "native_text.pdf", settings)


def test_timed_out_pdf_is_not_silently_downgraded_to_the_legacy_parser():
    """The timeout is operator policy, like the size and page limits."""
    from docling.datamodel.base_models import FailureCategory

    settings = Settings(docling_enabled=True)
    result = _partial_result(FailureCategory.TIMEOUT)
    with patch.object(
        docling_loader, "_converter", return_value=_stub_converter(result)
    ):
        with pytest.raises(docling_loader.DoclingLimitExceeded):
            load_document(FIXTURES / "native_text.pdf", settings)


def test_partial_conversion_without_timeout_is_a_conversion_error():
    """Any partial result is incomplete text; it must never pass as success."""
    from docling.datamodel.base_models import FailureCategory

    settings = Settings(docling_enabled=True)
    result = _partial_result(FailureCategory.BACKEND_FAILURE)
    with patch.object(
        docling_loader, "_converter", return_value=_stub_converter(result)
    ):
        with pytest.raises(docling_loader.DoclingConversionError):
            docling_loader.convert(FIXTURES / "native_text.pdf", settings)


def _doc_with_ocr_screenshot() -> DoclingDocument:
    """A page whose numbers exist only as OCR text nested inside a picture."""
    doc = DoclingDocument(name="synthetic-screenshot")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_heading(text="Manual Zendesk", level=1, prov=_prov(1))
    doc.add_text(
        label=DocItemLabel.TEXT,
        text=f"{PARAGRAPH} The ticket views are shown in the screenshot below.",
        prov=_prov(1),
    )
    picture = doc.add_picture(prov=_prov(1))
    for row, line in enumerate(
        (
            "Ticket Atendimento Telefonia 279",
            "Tickets Novos Atendimentos 74",
            "Tickets Abertos Atendimento 43",
        )
    ):
        top = 600 - 30 * row
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=line,
            prov=_prov_at(1, t=top, b=top - 12, left=100, r=300),
            parent=picture,
        )
    return doc


def test_ocr_text_inside_pictures_reaches_the_chunks():
    """Text OCR'd out of an embedded screenshot must be retrievable.

    Docling stores it as children of the PictureItem, which the default
    chunking serializer renders as an empty placeholder — losing exactly the
    text the OCR stage existed to recover.
    """
    chunks = chunk_document(
        _as_document(_doc_with_ocr_screenshot()),
        Settings(docling_enabled=True),
    )
    joined = "\n".join(c.text for c in chunks)
    assert "Ticket Atendimento Telefonia 279" in joined
    assert "Tickets Novos Atendimentos 74" in joined
    assert "Tickets Abertos Atendimento 43" in joined


def test_picture_chunk_with_ocr_text_is_marked_as_figure():
    chunks = chunk_document(
        _as_document(_doc_with_ocr_screenshot()),
        Settings(docling_enabled=True),
    )
    figure_chunks = [c for c in chunks if c.metadata["content_type"] == "figure"]
    assert figure_chunks
    assert any("279" in c.text for c in figure_chunks)


def _prov_at(page: int, *, t: float, b: float, left: float, r: float) -> ProvenanceItem:
    return ProvenanceItem(
        page_no=page,
        bbox=BoundingBox(l=left, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
        charspan=(0, 10),
    )


def test_ocr_lines_are_rebuilt_from_boxes_not_tree_order():
    """A label and its count OCR as separate boxes on one visual row.

    Kept apart they read as an unpaired list the LLM has to re-align (and
    demonstrably misaligns); merged by their boxes they read as the row a
    human sees: "Ticket Atendimento Telefonia 279".
    """
    doc = DoclingDocument(name="synthetic-rows")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    picture = doc.add_picture(prov=_prov(1))
    rows = [
        ("Ticket Atendimento Telefonia", "279", 609, 598),
        ("Tickets Novos Atendimentos", "74", 580, 568),
        ("Tickets Abertos Atendimento", "43", 550, 538),
    ]
    for label, count, top, bottom in rows:
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=label,
            prov=_prov_at(1, t=top, b=bottom, left=113, r=250),
            parent=picture,
        )
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=count,
            prov=_prov_at(1, t=top + 1, b=bottom, left=284, r=306),
            parent=picture,
        )
    chunks = chunk_document(_as_document(doc), Settings(docling_enabled=True))
    joined = "\n".join(c.text for c in chunks)
    assert "Ticket Atendimento Telefonia | 279" in joined
    assert "Tickets Novos Atendimentos | 74" in joined
    assert "Tickets Abertos Atendimento | 43" in joined
    # Distinct rows must not collapse into one line.
    assert "279 | Tickets Novos" not in joined
