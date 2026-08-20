"""Point lookups against the index, outside the retrieval pipeline.

sector_for_source answers "which sector does this document live in, as far as
this caller may know" — the feedback endpoint uses it to snapshot a report's
sector server-side instead of trusting whatever the client sends. The sector
filter is applied in the Qdrant query, not checked afterwards: a source
outside the caller's sectors must be indistinguishable from one that does not
exist, the same ambiguity the no_match refusal preserves.
"""

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from docquery.config import Settings


def _client(settings: Settings) -> QdrantClient:
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


def modified_for_sources(
    sources: list[str], settings: Settings, sectors: list[str] | None
) -> dict[str, str]:
    """modified_at per source, for the sources the caller may see.

    Read live from the index rather than snapshotted with the report: a
    re-ingest after the flag should show the new date — "has it been updated
    since?" is the question a reviewer is asking. A source that is missing,
    outside the caller's sectors or dateless is simply absent from the result;
    the same sector filter rides in the Qdrant query, never a check after.
    """
    if sectors is not None:
        sectors = [s for s in sectors if s]
        if not sectors:
            return {}
    if not sources:
        return {}

    client = _client(settings)
    existing = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in existing:
        return {}

    out: dict[str, str] = {}
    for source in sources:
        must = [FieldCondition(key="source", match=MatchValue(value=source))]
        if sectors:
            must.append(FieldCondition(key="sector", match=MatchAny(any=sectors)))
        points, _ = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=Filter(must=must),
            limit=1,
            with_payload=["modified_at"],
            with_vectors=False,
        )
        if points:
            value = (points[0].payload or {}).get("modified_at") or ""
            if value:
                out[source] = value
    return out


def sector_for_source(
    source: str, settings: Settings, sectors: list[str] | None
) -> str | None:
    """The sector of a source the caller may see, or None.

    None covers "no such document", "outside the caller's sectors" and "no
    collection yet" alike — the caller turns all of them into the same 404.
    Blanks are dropped and an emptied list short-circuits, the rule hybrid.py
    applies before every query.
    """
    if sectors is not None:
        sectors = [s for s in sectors if s]
        if not sectors:
            return None

    client = _client(settings)
    existing = {c.name for c in client.get_collections().collections}
    if settings.qdrant_collection not in existing:
        return None

    must = [FieldCondition(key="source", match=MatchValue(value=source))]
    if sectors:
        must.append(FieldCondition(key="sector", match=MatchAny(any=sectors)))
    points, _ = client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=Filter(must=must),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return None
    return (points[0].payload or {}).get("sector") or None
