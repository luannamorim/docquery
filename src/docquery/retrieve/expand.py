from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

from docquery.config import Settings


def expand_contexts(
    contexts: list[dict],
    client: QdrantClient,
    settings: Settings,
) -> list[dict]:
    """Expand each reranked context with adjacent chunks from the same source.

    For each context, fetches chunks at chunk_index in [idx-window, idx+window],
    sorts them, and concatenates their text. If two reranked contexts share the
    same expansion window (overlapping neighbors), the duplicate is dropped.
    """
    window = settings.context_expansion_window
    if window <= 0:
        return contexts

    seen: set[tuple[str, int, int]] = set()
    out: list[dict] = []
    for ctx in contexts:
        src = ctx["source"]
        idx = ctx["chunk_index"]
        lo, hi = idx - window, idx + window
        key = (src, lo, hi)
        if key in seen:
            continue
        seen.add(key)

        points, _ = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="source", match=MatchValue(value=src)),
                    FieldCondition(key="chunk_index", range=Range(gte=lo, lte=hi)),
                ]
            ),
            limit=2 * window + 1,
            with_payload=True,
            with_vectors=False,
        )
        ordered = sorted(points, key=lambda p: (p.payload or {}).get("chunk_index", 0))
        merged = "\n".join((p.payload or {}).get("text", "") for p in ordered)
        out.append({**ctx, "text": merged})
    return out
