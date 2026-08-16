"""A document that yields no chunks must not disappear quietly.

This is how a scanned contract went missing: the loader read it, extracted
twenty characters of nothing, produced zero chunks, and the ingest reported
success. Weeks later the answer to "which contracts do we have?" was a
confident, complete-sounding sentence about the one document that happened to
have a text layer.

That failure is worse than a crash, because nothing distinguishes "the corpus
does not contain it" from "the corpus was never told about it".
"""

import logging
from types import SimpleNamespace

from docquery.config import Settings
from docquery.ingest.pipeline import warn_about_empty_documents


def _doc(source: str) -> SimpleNamespace:
    return SimpleNamespace(metadata={"source": source})


def test_a_document_with_no_chunks_is_named_in_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        empty = warn_about_empty_documents(
            [_doc("data/contracts/db1_2023.pdf"), _doc("data/contracts/crk.pdf")],
            {"data/contracts/crk.pdf"},
            Settings(openai_api_key="sk-test"),
        )

    assert empty == ["data/contracts/db1_2023.pdf"]
    assert "db1_2023.pdf" in caplog.text


def test_the_warning_points_at_the_likely_cause(caplog):
    """Naming the file is not enough — the operator needs the next move.

    A PDF with no text layer is the overwhelmingly common case, and Docling
    with OCR is the switch that reads it.
    """
    with caplog.at_level(logging.WARNING):
        warn_about_empty_documents(
            [_doc("data/contracts/scan.pdf")],
            set(),
            Settings(openai_api_key="sk-test", docling_enabled=False),
        )

    assert "DOCLING_ENABLED" in caplog.text


def test_no_such_hint_when_docling_is_already_on(caplog):
    """Then OCR already ran and something else is wrong; suggesting the switch
    the operator already flipped would send them in a circle."""
    with caplog.at_level(logging.WARNING):
        warn_about_empty_documents(
            [_doc("data/contracts/scan.pdf")],
            set(),
            Settings(openai_api_key="sk-test", docling_enabled=True),
        )

    assert "scan.pdf" in caplog.text
    assert "DOCLING_ENABLED" not in caplog.text


def test_nothing_is_logged_when_every_document_produced_chunks(caplog):
    with caplog.at_level(logging.WARNING):
        empty = warn_about_empty_documents(
            [_doc("a.md")], {"a.md"}, Settings(openai_api_key="sk-test")
        )

    assert empty == []
    assert caplog.text == ""
