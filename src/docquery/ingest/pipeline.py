import argparse
import hashlib
import logging
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
from docquery.ingest.chunker import Chunk, chunk_document
from docquery.ingest.loader import iter_ingestable_files, load_directory, load_document
from docquery.ingest.sparse import sparse_vector
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
        client.create_payload_index(
            collection_name=settings.qdrant_collection,
            field_name="clearance_level",
            field_schema=PayloadSchemaType.INTEGER,
        )
        # Filterable taxonomy/facets (doc_type is server-side classified; entity
        # and tags are descriptive). KEYWORD indexes also cover array values.
        for field_name in ("doc_type", "entity", "tags"):
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
    sparse_vectors = [sparse_vector(t) for t in texts]

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
                "clearance_level": int(chunk.metadata.get("clearance_level", 0)),
                "doc_type": chunk.metadata.get("doc_type", ""),
                "entity": chunk.metadata.get("entity", ""),
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


def _apply_clearance_policy(docs: list, settings: Settings) -> None:
    """Set clearance_level on each doc based on settings.clearance_policy.

    Policy entries are (path_prefix, level); the first matching prefix wins.
    Unmatched documents fall back to settings.default_clearance_level. The
    frontmatter `clearance` field is intentionally ignored — classification is
    server-side only so untrusted authors cannot self-label sensitive content.
    """
    for doc in docs:
        source = str(doc.metadata.get("source", ""))
        level = settings.default_clearance_level
        for prefix, lvl in settings.clearance_policy:
            if source.startswith(prefix):
                level = lvl
                break
        doc.metadata["clearance_level"] = level
        logger.info("Clearance applied: source=%s level=%d", source, level)


def _apply_type_policy(docs: list, settings: Settings) -> None:
    """Set doc_type on each doc based on settings.type_policy.

    Policy entries are (path_prefix, doc_type); the first matching prefix wins.
    Unmatched documents fall back to settings.default_doc_type. As with
    clearance, the type is classified server-side by path so untrusted authors
    cannot self-label content that gates retrieval scope.
    """
    for doc in docs:
        source = str(doc.metadata.get("source", ""))
        doc_type = settings.default_doc_type
        for prefix, name in settings.type_policy:
            if source.startswith(prefix):
                doc_type = name
                break
        doc.metadata["doc_type"] = doc_type
        logger.info("Doc type applied: source=%s doc_type=%s", source, doc_type)


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
    _apply_clearance_policy(docs, settings)
    _apply_type_policy(docs, settings)

    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, settings=settings))

    sources_to_ingest = {doc.metadata.get("source", "") for doc in docs} - {""}
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
        orphan_prefix = orphan_prefix_for(path)
    else:
        current_sources = set()
        docs = [load_document(path, settings=settings)]
        orphan_prefix = None

    logger.info("Loaded %d document(s) from %s", len(docs), path)
    return _ingest_documents(docs, client, settings, current_sources, orphan_prefix)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into Qdrant")
    parser.add_argument("path", type=Path, help="File or directory to ingest")
    args = parser.parse_args()

    if not args.path.exists():
        parser.error(f"Path does not exist: {args.path}")

    settings = get_settings()
    result = ingest_path(args.path, settings=settings)
    print(
        f"Ingested {result['chunks']} chunks from {args.path}"
        f" (deleted {result['deleted']} orphan source(s))"
    )


if __name__ == "__main__":
    main()
