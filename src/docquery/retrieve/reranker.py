from qdrant_client.models import ScoredPoint
from sentence_transformers import CrossEncoder

from docquery.config import Settings, get_settings

_reranker: CrossEncoder | None = None
_reranker_model: str = ""


def _get_reranker(model_name: str) -> CrossEncoder:
    global _reranker, _reranker_model
    if _reranker is None or _reranker_model != model_name:
        _reranker = CrossEncoder(model_name)
        _reranker_model = model_name
    return _reranker


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

    texts = [p.payload.get("text", "") for p in points]  # type: ignore[union-attr]
    reranker = _get_reranker(settings.reranker_model)
    ranked = reranker.rank(
        query,
        texts,
        top_k=settings.reranker_top_k,
        return_documents=False,
    )

    return [
        {
            "text": points[r["corpus_id"]].payload.get("text", ""),  # type: ignore[union-attr]
            "source": points[r["corpus_id"]].payload.get("source", ""),  # type: ignore[union-attr]
            "chunk_index": points[r["corpus_id"]].payload.get("chunk_index", 0),  # type: ignore[union-attr]
            "score": float(r["score"]),
        }
        for r in ranked
    ]
