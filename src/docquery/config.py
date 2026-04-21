from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection: str = "documents"

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # Reranker
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 5
    reranker_score_threshold: float = -5.0

    # Retrieval
    retrieval_top_k: int = 20

    # Chunking
    chunk_size: int = 1024
    chunk_overlap: int = 100

    # Heading promotion for non-markdown procedural docs.
    # Patterns that match at line start are rewritten as "## ..." so the
    # markdown pipeline can extract them as sections.
    heading_patterns: list[str] = [
        r"^Passo \d+[:.]",
        r"^Step \d+[:.]",
    ]

    # Context expansion — fetch N neighbor chunks on each side of each reranked result
    context_expansion_window: int = 1

    # LLM
    openai_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
