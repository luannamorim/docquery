from functools import lru_cache

import numpy as np
from sentence_transformers import SentenceTransformer

from docquery.config import Settings, get_settings


@lru_cache(maxsize=4)
def _get_model(model_name: str) -> SentenceTransformer:
    return SentenceTransformer(model_name)


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
