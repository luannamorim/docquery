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
    ScoredPoint,
    SparseVector,
)

from docquery.config import Settings, get_settings
from docquery.folders import normalize_segment
from docquery.ingest.sparse import sparse_vector
from docquery.retrieve.embedder import embed_texts


def retrieve(
    query: str,
    client: QdrantClient,
    settings: Settings | None = None,
    sectors: list[str] | None = None,
    folders: list[str] | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
) -> list[ScoredPoint]:
    """Hybrid retrieval using dense + BM25 sparse vectors with RRF fusion.

    Returns up to settings.retrieval_top_k scored points from Qdrant that the
    caller's sectors allow, each with a .payload containing "text", "source",
    "chunk_index", "file_type", "section", "sector", "folders", "entity",
    "tags".

    sectors is the access compartment, imposed by the server rather than chosen
    by the caller: None skips the filter (no auth to enforce), [] returns
    nothing, and a list restricts to those sectors.

    Optional scoping filters are ANDed with the sector filter:
    - folders: restrict to sources under any of these folder names, matched at
      any depth of the ingested tree (e.g. ["rh"])
    - source: restrict to a single source document path
    - tags: restrict to chunks carrying any of these tags
    """
    settings = settings or get_settings()

    # The sector compartment is imposed, not requested: sectors is None only
    # when there is no identity to enforce (auth off). Anything else narrows,
    # and narrowing to nothing short-circuits before the query is even embedded
    # — an empty MatchAny is not a dependable way to say "match none".
    #
    # Blanks are dropped here rather than trusted from the callers: a document
    # at the ingest root carries sector "", so a stray "" would match exactly
    # the documents that belong to no compartment.
    if sectors is not None:
        sectors = [s for s in sectors if s]
        if not sectors:
            return []

    existing = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in existing:
        return []

    dense_vec = embed_texts([query], settings=settings, role="query")[0].tolist()
    sparse_indices, sparse_values = sparse_vector(query)

    conditions: list[Condition] = []
    if sectors:
        conditions.append(FieldCondition(key="sector", match=MatchAny(any=sectors)))
    if folders:
        # Normalized here, the single choke point every caller passes through, so
        # a filter matches the folder name as the user sees it regardless of case.
        wanted = [s for f in folders if (s := normalize_segment(f))]
        if wanted:
            conditions.append(FieldCondition(key="folders", match=MatchAny(any=wanted)))
    if source:
        conditions.append(FieldCondition(key="source", match=MatchValue(value=source)))
    if tags:
        conditions.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
    # None rather than Filter(must=[]): with auth off and no scoping filters
    # there is nothing to constrain.
    query_filter = Filter(must=conditions) if conditions else None

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
