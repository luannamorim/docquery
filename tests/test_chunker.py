from docquery.config import Settings
from docquery.ingest.chunker import Chunk, chunk_document
from docquery.ingest.loader import Document


def _settings(**overrides) -> Settings:
    return Settings(**{"openai_api_key": "test", **overrides})


def test_chunk_short_text_returns_single_chunk() -> None:
    doc = Document(
        content="Short text.", metadata={"source": "f.txt", "file_type": ".txt"}
    )
    chunks = chunk_document(doc, settings=_settings())
    assert len(chunks) == 1
    assert chunks[0].text == "Short text."


def test_chunk_metadata_propagated() -> None:
    doc = Document(
        content="Some content.", metadata={"source": "f.md", "file_type": ".md"}
    )
    chunks = chunk_document(doc, settings=_settings())
    assert chunks[0].metadata["source"] == "f.md"
    assert chunks[0].metadata["file_type"] == ".md"
    assert chunks[0].metadata["chunk_index"] == 0


def test_chunk_long_text_splits() -> None:
    doc = Document(
        content="word " * 500,
        metadata={"source": "f.txt", "file_type": ".txt"},
    )
    chunks = chunk_document(doc, settings=_settings(chunk_size=100, chunk_overlap=10))
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 100 + 10  # allow some margin


def test_chunk_indices_sequential() -> None:
    doc = Document(
        content="paragraph one\n\n" * 50,
        metadata={"source": "f.txt", "file_type": ".txt"},
    )
    chunks = chunk_document(doc, settings=_settings(chunk_size=100, chunk_overlap=10))
    indices = [c.metadata["chunk_index"] for c in chunks]
    assert indices == list(range(len(chunks)))


def test_markdown_splitter_used_for_md() -> None:
    content = "# Header 1\n\nParagraph under header 1.\n\n" * 20
    doc = Document(content=content, metadata={"source": "f.md", "file_type": ".md"})
    chunks = chunk_document(doc, settings=_settings(chunk_size=100, chunk_overlap=10))
    assert len(chunks) > 1
    # Markdown splitter should keep headers with their content
    assert any("# Header 1" in c.text for c in chunks)


def test_chunk_dataclass_fields() -> None:
    chunk = Chunk(text="hello", metadata={"key": "val"})
    assert chunk.text == "hello"
    assert chunk.metadata == {"key": "val"}


def test_chunk_empty_content() -> None:
    doc = Document(content="", metadata={"source": "f.txt", "file_type": ".txt"})
    chunks = chunk_document(doc, settings=_settings())
    assert chunks == []


def test_markdown_section_extracted_from_header() -> None:
    content = "## Passo 1: Configurar ambiente\n\n" + "detalhe " * 20
    doc = Document(content=content, metadata={"source": "f.md", "file_type": ".md"})
    chunks = chunk_document(doc, settings=_settings())
    assert chunks[0].metadata["section"] == "Passo 1: Configurar ambiente"


def test_markdown_section_inherited_by_following_chunks() -> None:
    content = (
        "## Instalação\n\n"
        + "passo " * 200
        + "\n\ncontinuação sem header " * 50
    )
    doc = Document(content=content, metadata={"source": "f.md", "file_type": ".md"})
    chunks = chunk_document(doc, settings=_settings(chunk_size=100, chunk_overlap=10))
    assert len(chunks) > 1
    assert all(c.metadata["section"] == "Instalação" for c in chunks)


def test_non_markdown_chunks_have_no_section() -> None:
    doc = Document(
        content="plain text " * 100,
        metadata={"source": "f.txt", "file_type": ".txt"},
    )
    chunks = chunk_document(doc, settings=_settings(chunk_size=100, chunk_overlap=10))
    assert all("section" not in c.metadata for c in chunks)


def test_chunks_pick_up_new_section_when_header_appears() -> None:
    content = (
        "## Seção A\n\n"
        + "conteúdo A " * 50
        + "\n\n## Seção B\n\n"
        + "conteúdo B " * 50
    )
    doc = Document(content=content, metadata={"source": "f.md", "file_type": ".md"})
    chunks = chunk_document(doc, settings=_settings(chunk_size=200, chunk_overlap=20))
    sections = [c.metadata["section"] for c in chunks]
    assert "Seção A" in sections
    assert "Seção B" in sections
    last_a = max(i for i, s in enumerate(sections) if s == "Seção A")
    first_b = min(i for i, s in enumerate(sections) if s == "Seção B")
    assert last_a < first_b, f"chunks after B inherited A: {sections}"
