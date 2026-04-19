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
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings, get_settings
from docquery.ingest.chunker import Chunk, chunk_document
from docquery.ingest.loader import LOADERS, load_directory, load_document
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
            id=int(hashlib.sha256((chunk.text + chunk.metadata.get("source", "")).encode()).hexdigest()[:16], 16),
            vector={
                "dense": dense,
                "sparse": SparseVector(indices=indices, values=values),
            },
            payload={
                "text": chunk.text,
                "source": chunk.metadata.get("source", ""),
                "chunk_index": int(chunk.metadata.get("chunk_index", 0)),
                "file_type": chunk.metadata.get("file_type", ""),
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


def delete_orphan_chunks(
    client: QdrantClient,
    settings: Settings,
    directory: Path,
    current_sources: set[str],
) -> int:
    """Delete chunks whose source file no longer exists under directory. Returns deleted source count."""
    prefix = str(directory)
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


def ingest_path(path: Path, settings: Settings | None = None) -> dict[str, int]:
    """Ingest a file or directory into Qdrant. Returns chunk and deleted counts."""
    settings = settings or get_settings()
    client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    ensure_collection(client, settings)

    if path.is_dir():
        current_sources = {str(f) for f in path.iterdir() if f.suffix.lower() in LOADERS}
        docs = load_directory(path)
    else:
        current_sources = set()
        docs = [load_document(path)]

    logger.info("Loaded %d document(s) from %s", len(docs), path)

    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, settings=settings))

    ingest_chunks(all_chunks, client, settings)
    logger.info("Ingested %d chunks into collection '%s'", len(all_chunks), settings.qdrant_collection)

    deleted = delete_orphan_chunks(client, settings, path, current_sources) if path.is_dir() else 0
    return {"chunks": len(all_chunks), "deleted": deleted}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into Qdrant")
    parser.add_argument("path", type=Path, help="File or directory to ingest")
    args = parser.parse_args()

    if not args.path.exists():
        parser.error(f"Path does not exist: {args.path}")

    settings = get_settings()
    result = ingest_path(args.path, settings=settings)
    print(f"Ingested {result['chunks']} chunks from {args.path} (deleted {result['deleted']} orphan source(s))")


if __name__ == "__main__":
    main()
