"""A dimension that disagrees with the model is silently broken search.

Qdrant accepts any vector of the declared collection size, so nothing errors
— retrieval just returns garbage. Fail at boot instead, like history and
feedback do.
"""

import pytest

from docquery.config import Settings


def test_a_known_model_with_the_wrong_dimension_fails_at_boot():
    with pytest.raises(ValueError, match="multilingual-e5-base"):
        Settings(
            embedding_model="intfloat/multilingual-e5-base", embedding_dimension=384
        )


def test_a_known_model_with_its_own_dimension_boots():
    settings = Settings(
        embedding_model="intfloat/multilingual-e5-base", embedding_dimension=768
    )
    assert settings.embedding_dimension == 768


def test_an_unknown_model_passes_with_any_dimension():
    """The table cannot know every model; an unknown one is the operator's
    responsibility, not a boot failure."""
    settings = Settings(embedding_model="acme/embedder-x", embedding_dimension=123)
    assert settings.embedding_dimension == 123
