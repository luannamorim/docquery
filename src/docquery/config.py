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

    # Retrieval
    retrieval_top_k: int = 20

    # Chunking
    chunk_size: int = 512
    chunk_overlap: int = 50

    # LLM
    openai_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
