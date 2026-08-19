import argparse
import hashlib
import logging
import tempfile
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings, get_settings
from docquery.folders import folder_segments, sector_of
from docquery.ingest.chunker import Chunk, chunk_document
from docquery.ingest.loader import (
    is_skippable_load_error,
    iter_ingestable_files,
    load_directory,
    load_document,
)
from docquery.ingest.sources import SourceError, fetch, source_scheme, validate_uri
from docquery.ingest.sparse import document_terms, sparse_vector
from docquery.retrieve.embedder import embed_texts

logger = logging.getLogger(__name__)


def ensure_collection(client: QdrantClient, settings: Settings) -> None:
    """Create the Qdrant collection if it doesn't exist."""
    existing = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in existing:
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config={
                "dense": VectorParams(
                    size=settings.embedding_dimension,
                    distance=Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(modifier=Modifier.IDF),
            },
        )
        # Filterable taxonomy/facets. sector is the access compartment; folders
        # is the search facet derived from the same tree; entity and tags are
        # descriptive. KEYWORD indexes also cover array values, so each folder
        # segment is matchable on its own.
        for field_name in ("sector", "folders", "entity", "tags"):
            client.create_payload_index(
                collection_name=settings.qdrant_collection,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )


def ingest_chunks(
    chunks: list[Chunk],
    client: QdrantClient,
    settings: Settings,
) -> None:
    """Embed chunks and upsert to Qdrant with dense + sparse vectors."""
    before = len(chunks)
    chunks = [c for c in chunks if c.text.strip()]
    dropped = before - len(chunks)
    if dropped:
        logger.warning("Dropped %d empty chunk(s) before upsert", dropped)
    if not chunks:
        return

    texts = [c.text for c in chunks]
    dense_vectors = embed_texts(texts, settings=settings).tolist()

    # The document's name and folders join the lexical index but not the text.
    # Without them, most chunks of a contract carry nothing saying which
    # contract they are — see document_terms. Dense vectors are left alone: a
    # file name is a label, and averaging it into the passage's meaning would
    # blur the passage.
    def _lexical(chunk: Chunk) -> str:
        terms = document_terms(
            str(chunk.metadata.get("source", "")),
            list(chunk.metadata.get("folders") or []),
        )
        return terms + " " + chunk.text

    sparse_vectors = [sparse_vector(_lexical(c)) for c in chunks]

    points = [
        PointStruct(
            id=int(
                hashlib.sha256(
                    "\x00".join(
                        [
                            str(chunk.metadata.get("source", "")),
                            str(chunk.metadata.get("chunk_index", 0)),
                            chunk.text,
                        ]
                    ).encode()
                    # 16 hex chars = 64 bits. Qdrant integer point IDs must fit an
                    # unsigned 64-bit int; a wider slice (e.g. 128 bits) is rejected
                    # with 400 "not a valid point ID". Matches the test helpers.
                ).hexdigest()[:16],
                16,
            ),
            vector={
                "dense": dense,
                "sparse": SparseVector(indices=indices, values=values),
            },
            payload={
                "text": chunk.text,
                "source": chunk.metadata.get("source", ""),
                "chunk_index": int(chunk.metadata.get("chunk_index", 0)),
                "file_type": chunk.metadata.get("file_type", ""),
                "section": chunk.metadata.get("section", ""),
                "title": chunk.metadata.get("title", ""),
                # Page provenance from Docling; 0 means the format has no pages
                # (DOCX/PPTX/XLSX) or the chunk carries no provenance.
                "page_number": int(chunk.metadata.get("page_number", 0)),
                # text | table | figure — "text" for the legacy parsers.
                "content_type": chunk.metadata.get("content_type", "text"),
                "folders": chunk.metadata.get("folders", []),
                # Access compartment. "" means no role can reach the chunk.
                "sector": chunk.metadata.get("sector", ""),
                "entity": chunk.metadata.get("entity", ""),
                # When the document was last updated, per the library it lives
                # in or the metadata inside the file. "" means no source knew —
                # never the ingest time, which is what mtime would have given.
                "modified_at": chunk.metadata.get("modified_at", ""),
                "tags": chunk.metadata.get("tags", []),
            },
        )
        for chunk, dense, (indices, values) in zip(
            chunks, dense_vectors, sparse_vectors
        )
    ]

    batch_size = 100
    for i in range(0, len(points), batch_size):
        client.upsert(
            collection_name=settings.qdrant_collection,
            points=points[i : i + batch_size],
        )


def orphan_prefix_for(location: Path | str) -> str:
    """Bound a container's source prefix with a separator.

    Sources are matched by prefix, so an unterminated "docs/sample" also claims
    "docs/sample-old/..." and would delete it as an orphan. Works the same for
    remote URIs, whose sources are "<folder-uri>/<relative path>".
    """
    text = str(location)
    return text if text.endswith("/") else text + "/"


def delete_orphan_chunks(
    client: QdrantClient,
    settings: Settings,
    prefix: str,
    current_sources: set[str],
) -> int:
    """Delete chunks under prefix whose source is gone. Returns deleted count."""
    indexed_sources: set[str] = set()
    offset = None

    while True:
        results, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            limit=250,
            offset=offset,
            with_payload=["source"],
            with_vectors=False,
        )
        for point in results:
            source = point.payload.get("source", "") if point.payload else ""
            if source.startswith(prefix):
                indexed_sources.add(source)
        if offset is None:
            break

    orphans = indexed_sources - current_sources
    for source in orphans:
        client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="source", match=MatchValue(value=source))]
                )
            ),
        )

    if orphans:
        logger.info("Deleted chunks for %d orphan source(s): %s", len(orphans), orphans)
    return len(orphans)


def delete_chunks_for_sources(
    client: QdrantClient,
    settings: Settings,
    sources: set[str],
) -> None:
    """Delete all existing chunks for the given sources before re-ingesting."""
    for source in sources:
        client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="source", match=MatchValue(value=source))]
                )
            ),
        )


def _place_document(doc, relative_path: str) -> None:
    """Derive the document's search facets and its access compartment.

    Both come from the same path relative to the ingested root, so they are set
    together at the entry points that know that root.
    """
    segments = folder_segments(relative_path)
    doc.metadata["folders"] = segments
    doc.metadata["sector"] = sector_of(segments)
    if not doc.metadata["sector"]:
        logger.warning(
            "No sector for %s: a document at the ingest root belongs to no "
            "compartment, so no role can reach it",
            doc.metadata.get("source", ""),
        )


def _qdrant_client(settings: Settings) -> QdrantClient:
    return QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=(
            settings.qdrant_api_key.get_secret_value()
            if settings.qdrant_api_key
            else None
        ),
        # Qdrant runs plaintext HTTP on the internal docker network. Passing an
        # api_key makes qdrant-client default to https=True, which fails the TLS
        # handshake against the non-TLS server. Keep the connection on HTTP.
        https=False,
    )


def warn_about_empty_documents(
    docs: list, chunked_sources: set[str], settings: Settings
) -> list[str]:
    """Name every document that was read but produced nothing. Returns them.

    A document that yields no chunks is indexed as if it did not exist, and the
    ingest otherwise reports success — so the corpus quietly lacks a file the
    operator watched it process. Retrieval then answers questions about that
    document with a confident, complete-sounding "there is no such thing",
    which is the worst shape this failure could take.

    The overwhelmingly common cause is a scanned PDF: pages of images with no
    text layer, which the legacy parsers cannot read. Docling with OCR can, so
    the warning says so — but only when it is off, since telling an operator to
    flip a switch they already flipped sends them in a circle.
    """
    empty = [
        source
        for doc in docs
        if (source := doc.metadata.get("source", "")) and source not in chunked_sources
    ]
    if not empty:
        return []

    for source in empty:
        logger.warning("No text extracted from %s — it will not be searchable", source)
    if not settings.docling_enabled:
        logger.warning(
            "%d document(s) produced no text. Scanned PDFs need OCR: set "
            "DOCLING_ENABLED=true and ingest again.",
            len(empty),
        )
    return empty


def _ingest_documents(
    docs: list,
    client: QdrantClient,
    settings: Settings,
    current_sources: set[str],
    orphan_prefix: str | None,
) -> dict[str, int]:
    """Classify, chunk and upsert documents; prune orphans under orphan_prefix.

    Shared by every ingest entry point, so local directories and remote folders
    get identical classification, deduplication and orphan semantics. A None
    orphan_prefix means the caller ingested a single document and nothing else
    under it should be pruned.
    """
    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, settings=settings))

    sources_to_ingest = {doc.metadata.get("source", "") for doc in docs} - {""}
    warn_about_empty_documents(
        docs, {c.metadata.get("source", "") for c in all_chunks}, settings
    )
    delete_chunks_for_sources(client, settings, sources_to_ingest)
    ingest_chunks(all_chunks, client, settings)
    logger.info(
        "Ingested %d chunks into collection '%s'",
        len(all_chunks),
        settings.qdrant_collection,
    )

    deleted = (
        delete_orphan_chunks(client, settings, orphan_prefix, current_sources)
        if orphan_prefix is not None
        else 0
    )
    return {"chunks": len(all_chunks), "deleted": deleted}


def ingest_path(path: Path, settings: Settings | None = None) -> dict[str, int]:
    """Ingest a local file or directory into Qdrant. Returns chunk/deleted counts."""
    settings = settings or get_settings()
    client = _qdrant_client(settings)
    ensure_collection(client, settings)

    if path.is_dir():
        current_sources = {str(f) for f in iter_ingestable_files(path, settings)}
        docs = load_directory(path, settings=settings)
        for doc in docs:
            relative = Path(str(doc.metadata["source"])).relative_to(path)
            _place_document(doc, relative.as_posix())
        orphan_prefix = orphan_prefix_for(path)
    else:
        current_sources = set()
        docs = [load_document(path, settings=settings)]
        # A single file is its own root: no folder structure, no compartment.
        _place_document(docs[0], path.name)
        orphan_prefix = None

    logger.info("Loaded %d document(s) from %s", len(docs), path)
    return _ingest_documents(docs, client, settings, current_sources, orphan_prefix)


def ingest_source(source: str, settings: Settings | None = None) -> dict[str, int]:
    """Ingest a local path or a remote folder URI. Returns chunk/deleted counts.

    Remote folders are pulled into a temporary directory, parsed by the ordinary
    loaders and discarded. Each document is indexed under its source URI rather
    than the scratch path it was written to, so deduplication, orphan pruning and
    the prefix policies behave exactly as they do for local files.
    """
    settings = settings or get_settings()
    if source_scheme(source) is None:
        return ingest_path(Path(source), settings=settings)

    # Up front, so a malformed URI or missing credential is reported as itself
    # rather than as whatever the run happens to hit first.
    validate_uri(source, settings)

    client = _qdrant_client(settings)
    ensure_collection(client, settings)

    base = source.rstrip("/")
    with tempfile.TemporaryDirectory(prefix="docquery-ingest-") as tmpdir:
        fetched = fetch(source, Path(tmpdir), settings)
        docs = []
        for item in fetched:
            try:
                doc = load_document(item.local_path, settings=settings)
            except Exception as e:
                if is_skippable_load_error(e, settings):
                    logger.error("Skipping %s: %s", item.source_uri, e)
                    continue
                raise
            doc.metadata["source"] = item.source_uri
            # The library recorded the edit; the file records whoever exported
            # it. Only overwrite when the library actually knows, so a document
            # the API has no date for keeps the one it carries.
            if item.modified_at:
                doc.metadata["modified_at"] = item.modified_at
            # Fetchers build source_uri as f"{base}/{relative}", so stripping the
            # base leaves exactly the path relative to the folder that was asked for.
            _place_document(doc, item.source_uri[len(base) + 1 :])
            docs.append(doc)

        logger.info("Loaded %d document(s) from %s", len(docs), source)
        return _ingest_documents(
            docs,
            client,
            settings,
            {item.source_uri for item in fetched},
            orphan_prefix_for(source),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into Qdrant")
    parser.add_argument(
        "source",
        help=(
            "File or directory to ingest, or a remote folder URI "
            "(sharepoint://<host>/sites/<site>/<drive>[/<folder>], "
            "gdrive://<folder id>)"
        ),
    )
    args = parser.parse_args()

    # Only local sources are checked here; a remote URI is validated by its
    # fetcher, which is the only thing that can tell whether it resolves.
    if source_scheme(args.source) is None and not Path(args.source).exists():
        parser.error(f"Path does not exist: {args.source}")

    settings = get_settings()
    try:
        result = ingest_source(args.source, settings=settings)
    except SourceError as e:
        # A bad URI or a missing credential is operator error, not a crash.
        parser.error(str(e))
    print(
        f"Ingested {result['chunks']} chunks from {args.source}"
        f" (deleted {result['deleted']} orphan source(s))"
    )


if __name__ == "__main__":
    main()
