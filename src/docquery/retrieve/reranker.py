from functools import lru_cache

from qdrant_client.models import ScoredPoint
from sentence_transformers import CrossEncoder

from docquery.config import Settings, get_settings


@lru_cache(maxsize=4)
def _get_reranker(model_name: str) -> CrossEncoder:
    return CrossEncoder(model_name)


def rerank(
    query: str,
    points: list[ScoredPoint],
    settings: Settings | None = None,
) -> list[dict]:
    """Rerank retrieved points with a cross-encoder.

    Returns up to settings.reranker_top_k dicts sorted by cross-encoder score:
    {"text": str, "source": str, "chunk_index": int, "score": float}
    """
    settings = settings or get_settings()
    if not points:
        return []

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
        }
        for r in ranked
        for payload in [(points[r["corpus_id"]].payload or {})]
    ]
    threshold = settings.reranker_score_threshold
    return [ctx for ctx in contexts if ctx["score"] >= threshold]
