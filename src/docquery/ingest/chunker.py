import re
from dataclasses import dataclass, field

from langchain_text_splitters import (
    MarkdownTextSplitter,
    RecursiveCharacterTextSplitter,
)

from docquery.config import Settings, get_settings
from docquery.ingest.loader import Document

_HEADER_RE = re.compile(r"^#{1,3}\s+(.+)", re.MULTILINE)


@dataclass
class Chunk:
    text: str
    metadata: dict[str, str | int] = field(default_factory=dict)


def chunk_document(doc: Document, settings: Settings | None = None) -> list[Chunk]:
    """Split a document into chunks using file-type-aware strategy.

    Markdown: MarkdownTextSplitter splits on headers and code blocks first.
    All other types: RecursiveCharacterTextSplitter as fixed-size fallback.
    """
    settings = settings or get_settings()

    if doc.metadata.get("file_type") == ".md":
        splitter = MarkdownTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
        texts = splitter.split_text(doc.content)
        chunks = []
        current_section = ""
        for i, t in enumerate(texts):
            m = _HEADER_RE.search(t)
            if m:
                current_section = m.group(1).strip()
            chunks.append(
                Chunk(text=t, metadata={**doc.metadata, "chunk_index": i, "section": current_section})
            )
        return chunks
    else:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
        )
        texts = splitter.split_text(doc.content)
        return [
            Chunk(text=t, metadata={**doc.metadata, "chunk_index": i})
            for i, t in enumerate(texts)
        ]
