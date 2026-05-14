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
    }


def rerank(
    query: str,
    points: list[ScoredPoint],
    settings: Settings | None = None,
) -> list[dict]:
    """Rerank retrieved points with a cross-encoder.

    Returns up to settings.reranker_top_k dicts sorted by cross-encoder score:
    {"text": str, "source": str, "chunk_index": int, "score": float}

    When settings.reranker_top_k <= 0 the cross-encoder is skipped entirely
    and the retrieved points are returned in their original (retrieval) order.
    This is the genuine "reranker off" path used by the ablation study.
    """
    settings = settings or get_settings()
    if not points:
        return []

    if settings.reranker_top_k <= 0:
        return [_point_to_context(p) for p in points]

    texts = [p.payload.get("text", "") if p.payload else "" for p in points]
    reranker = _get_reranker(settings.reranker_model)
    ranked = reranker.rank(
        query,
        texts,
        top_k=settings.reranker_top_k,
        return_documents=False,
    )

    contexts = [
        {
            "text": payload.get("text", ""),
            "source": payload.get("source", ""),
            "chunk_index": payload.get("chunk_index", 0),
            "score": float(r["score"]),
            "section": payload.get("section", ""),
        }
        for r in ranked
        for payload in [(points[r["corpus_id"]].payload or {})]
    ]
    threshold = settings.reranker_score_threshold
    return [ctx for ctx in contexts if ctx["score"] >= threshold]
