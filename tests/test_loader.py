from pathlib import Path

import pytest

from docquery.ingest.loader import Document, load_directory, load_document, load_text


def test_load_text(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    doc = load_text(f)
    assert doc.content == "hello world"
    assert doc.metadata["source"] == str(f)
    assert doc.metadata["file_type"] == ".txt"


def test_load_markdown(tmp_path: Path) -> None:
    f = tmp_path / "test.md"
    f.write_text("# Title\n\nParagraph.")
    doc = load_document(f)
    assert "# Title" in doc.content
    assert doc.metadata["file_type"] == ".md"


def test_load_unsupported_raises(tmp_path: Path) -> None:
    f = tmp_path / "test.csv"
    f.write_text("a,b,c")
    with pytest.raises(ValueError, match="Unsupported file type"):
        load_document(f)


def test_load_directory(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Doc A")
    (tmp_path / "b.txt").write_text("Doc B")
    (tmp_path / "c.csv").write_text("skip me")
    docs = load_directory(tmp_path)
    assert len(docs) == 2
    sources = {d.metadata["source"] for d in docs}
    assert str(tmp_path / "a.md") in sources
    assert str(tmp_path / "b.txt") in sources


def test_load_directory_empty(tmp_path: Path) -> None:
    assert load_directory(tmp_path) == []


def test_document_default_metadata() -> None:
    doc = Document(content="test")
    assert doc.metadata == {}


def test_text_with_heading_pattern_promotes_to_markdown(tmp_path: Path) -> None:
    f = tmp_path / "procedure.txt"
    f.write_text(
        "Instruções de deploy.\n\n"
        "Passo 1: Preparar ambiente\n\n"
        "Instale docker.\n\n"
        "Passo 2: Subir containers\n\n"
        "Rode docker-compose up -d.\n"
    )
    doc = load_document(f)
    assert doc.metadata["file_type"] == ".md"
    assert "## Passo 1:" in doc.content
    assert "## Passo 2:" in doc.content


def test_text_without_heading_pattern_stays_plain(tmp_path: Path) -> None:
    f = tmp_path / "prose.txt"
    f.write_text("Just a paragraph without any procedural structure.\n")
    doc = load_document(f)
    assert doc.metadata["file_type"] == ".txt"
    assert "## " not in doc.content


def test_heading_promotion_preserves_existing_markdown(tmp_path: Path) -> None:
    f = tmp_path / "guide.md"
    f.write_text("# Guia\n\nTexto normal sem passos numerados.\n")
    doc = load_document(f)
    assert doc.metadata["file_type"] == ".md"
    assert doc.content.startswith("# Guia")


def test_year_sentences_not_promoted_as_heading(tmp_path: Path) -> None:
    f = tmp_path / "history.txt"
    f.write_text(
        "1970. Assim como a geração que os antecedeu, as pessoas da Geração X\n"
        "cresceram em um mundo muito diferente.\n"
    )
    doc = load_document(f)
    assert doc.metadata["file_type"] == ".txt"
    assert "## " not in doc.content
