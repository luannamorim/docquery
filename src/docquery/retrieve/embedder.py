from functools import lru_cache
from typing import Literal

import numpy as np
from sentence_transformers import SentenceTransformer

from docquery.config import Settings, get_settings

# Models whose training used typed prefixes. The e5 family scores measurably
# worse when queries and passages are encoded bare; MiniLM and bge-m3 use
# none. The prefix is applied HERE, conditioned on the model — callers state
# a role and never spell a prefix, so a model swap cannot desynchronize the
# two sides of the search.
_ROLE_PREFIXES: dict[str, dict[str, str]] = {
    "intfloat/multilingual-e5-small": {"query": "query: ", "passage": "passage: "},
    "intfloat/multilingual-e5-base": {"query": "query: ", "passage": "passage: "},
    "intfloat/multilingual-e5-large": {"query": "query: ", "passage": "passage: "},
}


@lru_cache(maxsize=4)
def _get_model(model_name: str) -> SentenceTransformer:
    return SentenceTransformer(model_name)


def embed_texts(
    texts: list[str],
    settings: Settings | None = None,
    batch_size: int = 32,
    role: Literal["query", "passage"] = "passage",
) -> np.ndarray:
    """Encode texts into dense embeddings.

    role says which side of the search the texts are on: the pipeline embeds
    chunks as "passage", retrieval embeds the question as "query". For models
    with no prefix protocol the two are identical.

    Returns ndarray of shape (len(texts), embedding_dimension).
    """
    settings = settings or get_settings()
    prefixes = _ROLE_PREFIXES.get(settings.embedding_model)
    if prefixes:
        texts = [prefixes[role] + t for t in texts]
    model = _get_model(settings.embedding_model)
    return model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
