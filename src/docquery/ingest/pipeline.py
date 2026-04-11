import argparse
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings, get_settings
from docquery.ingest.chunker import Chunk, chunk_document
from docquery.ingest.loader import load_directory, load_document
from docquery.ingest.sparse import sparse_vector
from docquery.retrieve.embedder import embed_texts


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
    chunks = [c for c in chunks if c.text.strip()]
    if not chunks:
        return

    texts = [c.text for c in chunks]
    dense_vectors = embed_texts(texts, settings=settings).tolist()
    sparse_vectors = [sparse_vector(t) for t in texts]

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
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


def ingest_path(path: Path, settings: Settings | None = None) -> int:
    """Ingest a file or directory into Qdrant. Returns chunk count stored."""
    settings = settings or get_settings()
    client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    ensure_collection(client, settings)

    docs = load_directory(path) if path.is_dir() else [load_document(path)]

    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, settings=settings))

    ingest_chunks(all_chunks, client, settings)
    return len(all_chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into Qdrant")
    parser.add_argument("path", type=Path, help="File or directory to ingest")
    args = parser.parse_args()

    if not args.path.exists():
        parser.error(f"Path does not exist: {args.path}")

    settings = get_settings()
    count = ingest_path(args.path, settings=settings)
    print(f"Ingested {count} chunks from {args.path}")


if __name__ == "__main__":
    main()
