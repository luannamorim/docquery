"""Docling-backed parsing and chunking for the ingestion pipeline.

Docling replaces only the parsing stage: it turns a binary document into a
DoclingDocument, which this module maps onto the same Document/Chunk shapes the
legacy loaders produce. Everything downstream — sector derivation,
embedding, Qdrant payloads, retrieval — is untouched.

The module is imported lazily by loader.py so the docling package is never
loaded when settings.docling_enabled is False.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from docquery.config import Settings, get_settings

if TYPE_CHECKING:
    from pathlib import Path

    from docquery.ingest.chunker import Chunk
    from docquery.ingest.loader import Document

logger = logging.getLogger(__name__)

# Extensions routed to Docling when the feature flag is on. .txt and .md stay
# on the legacy path: Docling has no plain-text backend, and the markdown
# loader carries frontmatter parsing plus heading promotion that must not
# regress.
DOCLING_EXTENSIONS = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".pptx", ".xlsx"}
)

# Formats the legacy pipeline can also parse, so a Docling failure degrades to
# the old parser instead of failing the file.
FALLBACK_EXTENSIONS = frozenset({".pdf"})

# DoclingDocument item labels that decide a chunk's content_type.
_TABLE_LABELS = frozenset({"table", "document_index"})
_FIGURE_LABELS = frozenset({"picture", "chart"})

_BYTES_PER_MB = 1024 * 1024


class DoclingConversionError(RuntimeError):
    """Raised when Docling cannot parse a file that has no legacy fallback."""


class DoclingLimitExceeded(DoclingConversionError):
    """Raised when a document exceeds a configured conversion limit.

    Kept distinct from a parse failure because it is an operator policy, not a
    broken file: it must not be quietly downgraded to the legacy parser.
    """


@lru_cache(maxsize=4)
def _build_converter(
    do_ocr: bool,
    ocr_langs: tuple[str, ...],
    do_table_structure: bool,
    document_timeout: float,
    artifacts_path: str | None,
) -> Any:
    """Build a DocumentConverter. Cached — model loading is expensive.

    Keyed on primitives rather than the Settings object because Settings is not
    hashable.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import (
        DocumentConverter,
        ImageFormatOption,
        PdfFormatOption,
    )

    pipeline_options = PdfPipelineOptions(
        do_ocr=do_ocr,
        do_table_structure=do_table_structure,
        document_timeout=document_timeout,
        artifacts_path=artifacts_path,
    )
    if do_ocr:
        # Pin the engine instead of relying on auto-selection: RapidOCR on the
        # torch backend is what this image ships (torch is already a project
        # dependency, onnxruntime is not), and auto-selection would silently
        # change engine if another one ever appeared in the environment.
        pipeline_options.ocr_options = RapidOcrOptions(
            backend="torch", lang=list(ocr_langs)
        )

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
        }
    )


def _converter(settings: Settings) -> Any:
    return _build_converter(
        settings.docling_ocr_enabled,
        tuple(settings.docling_ocr_langs),
        settings.docling_table_structure,
        settings.docling_timeout_seconds,
        str(settings.docling_artifacts_path)
        if settings.docling_artifacts_path
        else None,
    )


def handles(path: Path, settings: Settings) -> bool:
    """Whether this path should be parsed by Docling under current settings."""
    return settings.docling_enabled and path.suffix.lower() in DOCLING_EXTENSIONS


def convert(path: Path, settings: Settings | None = None) -> Any:
    """Convert a file to a DoclingDocument, enforcing the configured limits."""
    settings = settings or get_settings()

    size_bytes = path.stat().st_size
    max_bytes = settings.docling_max_file_mb * _BYTES_PER_MB
    if size_bytes > max_bytes:
        raise DoclingLimitExceeded(
            f"{path} is {size_bytes / _BYTES_PER_MB:.1f} MB, over the "
            f"{settings.docling_max_file_mb} MB limit "
            f"(raise DOCLING_MAX_FILE_MB to allow it)"
        )

    _check_page_limit(path, settings)

    result = _converter(settings).convert(
        path,
        # Backstop for formats whose page count cannot be read up front.
        max_num_pages=settings.docling_max_pages,
        max_file_size=max_bytes,
    )
    _check_conversion_complete(path, result, settings)
    return result.document


def _check_conversion_complete(path: Path, result: Any, settings: Settings) -> None:
    """Reject a partial conversion instead of indexing the half that finished.

    Docling reports a mid-pipeline timeout (and other per-stage failures) as
    PARTIAL_SUCCESS on the result — it does not raise — so without this check
    a document whose OCR was cut off is indexed as if it converted cleanly,
    and every question about the missing text gets a confident wrong answer.
    """
    from docling.datamodel.base_models import ConversionStatus, FailureCategory

    if result.status == ConversionStatus.SUCCESS:
        return
    detail = (
        "; ".join(e.error_message for e in result.errors) or str(result.status)
    )
    if any(e.category == FailureCategory.TIMEOUT for e in result.errors):
        # Same contract as the size and page limits: a configured ceiling is
        # operator policy and must not degrade to the legacy parser, which
        # would drop the OCR text just as silently.
        raise DoclingLimitExceeded(
            f"{path} did not convert within DOCLING_TIMEOUT_SECONDS="
            f"{settings.docling_timeout_seconds:g}: {detail} "
            f"(raise DOCLING_TIMEOUT_SECONDS to allow it)"
        )
    raise DoclingConversionError(f"{path} converted only partially: {detail}")


def _check_page_limit(path: Path, settings: Settings) -> None:
    """Reject an over-long PDF before paying for any conversion work.

    Docling enforces the same cap internally, but only after starting the
    pipeline, and it reports the breach as a generic conversion failure —
    which the caller would then treat as a broken file and quietly downgrade
    to the legacy parser. Reading the page count up front costs a header parse
    and keeps the limit meaningful.
    """
    if path.suffix.lower() != ".pdf":
        return
    try:
        from pypdf import PdfReader

        page_count = len(PdfReader(path).pages)
    except Exception:
        # Unreadable header: let Docling decide what the file really is.
        logger.debug("Could not pre-read page count for %s", path, exc_info=True)
        return
    if page_count > settings.docling_max_pages:
        raise DoclingLimitExceeded(
            f"{path} has {page_count} pages, over the "
            f"{settings.docling_max_pages} page limit "
            f"(raise DOCLING_MAX_PAGES to allow it)"
        )


def load_with_docling(path: Path, settings: Settings | None = None) -> Document:
    """Parse a file with Docling into the same Document shape as the loaders.

    content is the markdown rendering (used for logging and by any non-Docling
    chunker); dl_doc carries the structured document so chunking can keep
    tables, headings and page provenance.
    """
    from docquery.ingest.loader import Document, MetaValue  # noqa: F401

    settings = settings or get_settings()
    dl_doc = convert(path, settings)

    meta: dict[str, MetaValue] = {
        "source": str(path),
        "file_type": path.suffix.lower(),
    }
    if getattr(dl_doc, "num_pages", None):
        try:
            meta["pages"] = str(dl_doc.num_pages())
        except TypeError:
            meta["pages"] = str(dl_doc.num_pages)

    return Document(
        content=dl_doc.export_to_markdown(),
        metadata=meta,
        dl_doc=dl_doc,
    )


def _serializer_provider() -> Any:
    """Serialize tables as markdown rather than Docling's default triplets.

    Markdown keeps the row/column grid readable both for the embedder and for a
    human reading the citation; the triplet form ("Basic, Price = 10") loses the
    visual structure.
    """
    from docling_core.transforms.chunker.hierarchical_chunker import (
        ChunkingDocSerializer,
        ChunkingSerializerProvider,
    )
    from docling_core.transforms.serializer.markdown import MarkdownTableSerializer

    class _MarkdownTableProvider(ChunkingSerializerProvider):
        def get_serializer(self, doc: Any) -> Any:
            return ChunkingDocSerializer(
                doc=doc,
                table_serializer=MarkdownTableSerializer(),
            )

    return _MarkdownTableProvider()


@lru_cache(maxsize=2)
def _chunker(embedding_model: str) -> Any:
    """Build a HybridChunker bound to the project's embedding tokenizer.

    Reusing the SentenceTransformer's own tokenizer and max_seq_length keeps
    chunk sizing consistent with what the embedder can actually encode, so
    chunks are no longer silently truncated at the model's token limit.
    """
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import (
        HuggingFaceTokenizer,
    )

    from docquery.retrieve.embedder import _get_model

    model = _get_model(embedding_model)
    return HybridChunker(
        tokenizer=HuggingFaceTokenizer(
            tokenizer=model.tokenizer,
            max_tokens=model.max_seq_length,
        ),
        # Tables larger than one chunk are split by rows with the header row
        # repeated on each fragment, so no fragment is a headerless grid.
        repeat_table_header=True,
        merge_peers=True,
        serializer_provider=_serializer_provider(),
    )


def _page_number(chunk: Any) -> int:
    """First page the chunk's content came from, or 0 when there is no page.

    Formats without pagination (DOCX, PPTX, XLSX) carry no provenance, so they
    report 0 — documented as "no page" rather than a misleading page 1.
    """
    for item in chunk.meta.doc_items:
        for prov in getattr(item, "prov", []) or []:
            if getattr(prov, "page_no", None):
                return int(prov.page_no)
    return 0


def _content_type(chunk: Any) -> str:
    """Classify a chunk as table, figure or text by the items it contains."""
    labels = {
        getattr(item.label, "value", str(item.label)) for item in chunk.meta.doc_items
    }
    if labels & _TABLE_LABELS:
        return "table"
    if labels & _FIGURE_LABELS:
        return "figure"
    return "text"


def chunk_with_docling(doc: Document, settings: Settings | None = None) -> list[Chunk]:
    """Chunk a Docling-parsed document, preserving structure and provenance."""
    from docquery.ingest.chunker import Chunk

    settings = settings or get_settings()
    chunker = _chunker(settings.embedding_model)

    chunks: list[Chunk] = []
    for chunk in chunker.chunk(doc.dl_doc):
        text = chunker.contextualize(chunk)
        headings = chunk.meta.headings or []
        chunks.append(
            Chunk(
                text=text,
                metadata={
                    **doc.metadata,
                    "chunk_index": len(chunks),
                    "section": " > ".join(headings),
                    "page_number": _page_number(chunk),
                    "content_type": _content_type(chunk),
                },
            )
        )
    return chunks
