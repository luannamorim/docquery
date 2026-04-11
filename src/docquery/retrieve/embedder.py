import numpy as np
from sentence_transformers import SentenceTransformer

from docquery.config import Settings, get_settings

_model: SentenceTransformer | None = None
_model_name: str = ""


def _get_model(model_name: str) -> SentenceTransformer:
    global _model, _model_name
    if _model is None or _model_name != model_name:
        _model = SentenceTransformer(model_name)
        _model_name = model_name
    return _model


def embed_texts(
    texts: list[str],
    settings: Settings | None = None,
    batch_size: int = 32,
) -> np.ndarray:
    """Encode texts into dense embeddings.

    Returns ndarray of shape (len(texts), embedding_dimension).
    """
    settings = settings or get_settings()
    model = _get_model(settings.embedding_model)
    return model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
