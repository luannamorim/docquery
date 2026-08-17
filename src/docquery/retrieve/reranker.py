from functools import lru_cache

from qdrant_client.models import ScoredPoint
from sentence_transformers import CrossEncoder

from docquery.config import Settings, get_settings


@lru_cache(maxsize=4)
def _get_reranker(model_name: str) -> CrossEncoder:
    return CrossEncoder(model_name)


def _point_to_context(point: ScoredPoint) -> dict:
    payload = point.payload or {}
    return {
        "text": payload.get("text", ""),
        "source": payload.get("source", ""),
        "chunk_index": payload.get("chunk_index", 0),
        "score": float(point.score),
        "section": payload.get("section", ""),
        "folders": payload.get("folders", []),
    }


def rerank(
    query: str,
    points: list[ScoredPoint],
    settings: Settings | None = None,
    prefer_sources: set[str] | None = None,
) -> list[dict]:
    """Rerank retrieved points with a cross-encoder.

    Returns up to settings.reranker_top_k dicts sorted by cross-encoder score:
    {"text": str, "source": str, "chunk_index": int, "score": float}

    prefer_sources are the documents the question named (see
    `retrieve/affinity.py`): their passages take the available slots first, in
    cross-encoder order, and everything else follows in cross-encoder order. The
    cross-encoder only ever sees `payload["text"]`, which for most chunks of a
    contract says nothing about which contract it is — so without this the
    document a question explicitly asked for loses to whichever document happens
    to phrase the topic best.

    When settings.reranker_top_k <= 0 the cross-encoder is skipped entirely
    and the retrieved points are returned in their original (retrieval) order.
    This is the genuine "reranker off" path used by the ablation study, so a
    preference is ignored there rather than turning it back on.
    """
    settings = settings or get_settings()
    if not points:
        return []

    if settings.reranker_top_k <= 0:
        return [_point_to_context(p) for p in points]

    texts = [p.payload.get("text", "") if p.payload else "" for p in points]
    reranker = _get_reranker(settings.reranker_model)
    # Every candidate, not top_k: the cut has to happen after the preference is
    # applied. A cross-encoder blind to document identity can rank the named
    # document's clause below the slot count, and promoting within an already
    # truncated list would never see it. `rank` scores all inputs whatever top_k
    # says, so asking for all of them costs nothing extra.
    ranked = reranker.rank(
        query,
        texts,
        top_k=len(texts),
        return_documents=False,
    )

    contexts = [
        {
            "text": payload.get("text", ""),
            "source": payload.get("source", ""),
            "chunk_index": payload.get("chunk_index", 0),
            "score": float(r["score"]),
            "section": payload.get("section", ""),
            "folders": payload.get("folders", []),
        }
        for r in ranked
        for payload in [(points[r["corpus_id"]].payload or {})]
    ]
    # Before the preference: naming a document decides which of the passages
    # worth sending gets a slot, never whether a passage nobody should read gets
    # one.
    #
    # Filtering the full list and truncating afterwards returns exactly what
    # truncating first and filtering afterwards used to, because `ranked` is
    # sorted by score: everything above the threshold is a prefix of it, so the
    # filter can never punch a hole for a lower-scoring passage to backfill.
    # That equality is what keeps the eval baseline comparable.
    threshold = settings.reranker_score_threshold
    contexts = [ctx for ctx in contexts if ctx["score"] >= threshold]

    if prefer_sources:
        named = [ctx for ctx in contexts if ctx["source"] in prefer_sources]
        rest = [ctx for ctx in contexts if ctx["source"] not in prefer_sources]
        contexts = named + rest

    return contexts[: settings.reranker_top_k]
