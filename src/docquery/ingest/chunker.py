from __future__ import annotations

from dataclasses import dataclass, field

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from docquery.config import Settings, get_settings
from docquery.ingest.loader import Document

_HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_BREADCRUMB_KEYS = ("h1", "h2", "h3")


@dataclass
class Chunk:
    text: str
    metadata: dict[str, str | int] = field(default_factory=dict)


def _breadcrumb(metadata: dict) -> str:
    return " > ".join(metadata[k] for k in _BREADCRUMB_KEYS if k in metadata)


def _chunk_markdown(doc: Document, settings: Settings) -> list[Chunk]:
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_HEADERS_TO_SPLIT_ON,
        strip_headers=False,
    )
    size_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    chunks: list[Chunk] = []
    for section in header_splitter.split_text(doc.content):
        breadcrumb = _breadcrumb(section.metadata)
        for sub in size_splitter.split_text(section.page_content):
            chunks.append(
                Chunk(
                    text=sub,
                    metadata={
                        **doc.metadata,
                        "chunk_index": len(chunks),
                        "section": breadcrumb,
                    },
                )
            )
    return chunks


def _chunk_recursive(doc: Document, settings: Settings) -> list[Chunk]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    texts = splitter.split_text(doc.content)
    return [
        Chunk(text=t, metadata={**doc.metadata, "chunk_index": i})
        for i, t in enumerate(texts)
    ]


def _chunk_semantic(doc: Document, settings: Settings) -> list[Chunk]:
    # Import lazily so langchain-experimental is optional for non-semantic runs
    try:
        from langchain_experimental.text_splitter import SemanticChunker
    except ImportError as e:
        raise ImportError(
            "langchain-experimental is required for chunker_strategy='semantic'. "
            "Install it with: uv sync --extra chunking"
        ) from e

    from docquery.retrieve.embedder import embed_texts

    class _EmbeddingsAdapter:
        """Minimal adapter so embed_texts works as a LangChain Embeddings object."""

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return embed_texts(texts, settings=settings).tolist()

        def embed_query(self, text: str) -> list[float]:
            return embed_texts([text], settings=settings)[0].tolist()

    chunker = SemanticChunker(
        embeddings=_EmbeddingsAdapter(),
        breakpoint_threshold_type=settings.semantic_breakpoint_threshold_type,
        breakpoint_threshold_amount=settings.semantic_breakpoint_threshold_amount,
    )
    texts = chunker.split_text(doc.content)
    return [
        Chunk(text=t, metadata={**doc.metadata, "chunk_index": i})
        for i, t in enumerate(texts)
    ]


def chunk_document(doc: Document, settings: Settings | None = None) -> list[Chunk]:
    """Split a document into chunks using the configured strategy.

    markdown  — header-aware split (H1/H2/H3) then size-based,
                preserves section breadcrumb
    recursive — plain size-based split, no section metadata
                (default fallback)
    semantic  — embedding-based split on semantic boundaries
                (requires langchain-experimental)
    """
    settings = settings or get_settings()

    match settings.chunker_strategy:
        case "markdown":
            if doc.metadata.get("file_type") == ".md":
                return _chunk_markdown(doc, settings)
            return _chunk_recursive(doc, settings)
        case "recursive":
            return _chunk_recursive(doc, settings)
        case "semantic":
            return _chunk_semantic(doc, settings)
        case _:
            raise ValueError(f"Unknown chunker_strategy: {settings.chunker_strategy!r}")
