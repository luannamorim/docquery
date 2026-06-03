from qdrant_client import QdrantClient
from qdrant_client.models import (
    Condition,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchAny,
    MatchValue,
    Prefetch,
    Range,
    ScoredPoint,
    SparseVector,
)

from docquery.config import Settings, get_settings
from docquery.ingest.sparse import sparse_vector
from docquery.retrieve.embedder import embed_texts


def retrieve(
    query: str,
    client: QdrantClient,
    settings: Settings | None = None,
    user_clearance: int = 0,
    doc_types: list[str] | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
) -> list[ScoredPoint]:
    """Hybrid retrieval using dense + BM25 sparse vectors with RRF fusion.

    Returns up to settings.retrieval_top_k scored points from Qdrant whose
    clearance_level <= user_clearance, each with a .payload containing
    "text", "source", "chunk_index", "file_type", "section", "clearance_level",
    "doc_type", "entity", "tags".

    Optional scoping filters are ANDed with the clearance filter:
    - doc_types: restrict to these document types (e.g. ["contract"])
    - source: restrict to a single source document path
    - tags: restrict to chunks carrying any of these tags
    """
    settings = settings or get_settings()

    existing = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in existing:
        return []

    dense_vec = embed_texts([query], settings=settings)[0].tolist()
    sparse_indices, sparse_values = sparse_vector(query)

    # clearance is always enforced; scoping filters are additive (must/AND).
    conditions: list[Condition] = [
        FieldCondition(key="clearance_level", range=Range(lte=user_clearance))
    ]
    if doc_types:
        conditions.append(FieldCondition(key="doc_type", match=MatchAny(any=doc_types)))
    if source:
        conditions.append(FieldCondition(key="source", match=MatchValue(value=source)))
    if tags:
        conditions.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
    query_filter = Filter(must=conditions)

    result = client.query_points(
        collection_name=settings.qdrant_collection,
        prefetch=[
            Prefetch(
                query=dense_vec,
                using="dense",
                limit=settings.retrieval_top_k,
                filter=query_filter,
            ),
            Prefetch(
                query=SparseVector(indices=sparse_indices, values=sparse_values),
                using="sparse",
                limit=settings.retrieval_top_k,
                filter=query_filter,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=settings.retrieval_top_k,
        with_payload=True,
    )

    return result.points
