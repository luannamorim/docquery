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


def chunk_document(doc: Document, settings: Settings | None = None) -> list[Chunk]:
    """Split a document into chunks using file-type-aware strategy.

    Markdown: split by H1/H2/H3 first so every chunk carries the full
    header breadcrumb (e.g. "Deploy > Passo 3 > 3.1 Sub") as section
    metadata, then split each section by size. All other types use a
    plain size-based splitter with no section metadata.
    """
    settings = settings or get_settings()

    if doc.metadata.get("file_type") == ".md":
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

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    texts = splitter.split_text(doc.content)
    return [
        Chunk(text=t, metadata={**doc.metadata, "chunk_index": i})
        for i, t in enumerate(texts)
    ]
