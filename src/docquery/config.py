from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection: str = "documents"
    qdrant_api_key: SecretStr | None = None

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # Reranker
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 8
    reranker_score_threshold: float = -5.0

    # Retrieval
    retrieval_top_k: int = 20

    # Chunking
    chunk_size: int = 1024
    chunk_overlap: int = 100
    chunker_strategy: Literal["markdown", "recursive", "semantic"] = "markdown"
    # SemanticChunker params (only used when chunker_strategy="semantic")
    semantic_breakpoint_threshold_type: Literal[
        "percentile", "standard_deviation", "interquartile", "gradient"
    ] = "percentile"
    semantic_breakpoint_threshold_amount: float = 95.0

    # Heading promotion for non-markdown procedural docs.
    # Patterns that match at line start are rewritten as "## ..." so the
    # markdown pipeline can extract them as sections.
    heading_patterns: list[str] = [
        r"^Passo \d+[:.]",
        r"^Step \d+[:.]",
    ]

    # Context expansion — fetch N neighbor chunks on each side of each reranked result
    context_expansion_window: int = 1

    # Ingest hardening — paths under this root only; symlinks pointing outside are rejected
    ingest_root: Path = Path("docs")

    # Clearance / RBAC — fail-closed default for documents without policy match
    default_clearance_level: int = 999
    max_clearance_level: int = 10
    # Path-prefix → clearance mapping applied at ingest time. Each entry is (path_prefix, level).
    # The first matching prefix wins; falls back to default_clearance_level when nothing matches.
    clearance_policy: list[tuple[str, int]] = []

    # Ingest task store
    task_ttl_seconds: int = 3600
    task_max_size: int = 1000

    # Guard
    guard_max_query_length: int = 2000

    # Rate limit / body cap
    rate_limit_requests_per_minute: int = 60
    request_max_body_bytes: int = 1_048_576

    # LLM
    openai_api_key: SecretStr = SecretStr("")
    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 2
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    llm_price_input_per_1m: float = 0.15
    llm_price_output_per_1m: float = 0.60


@lru_cache
def get_settings() -> Settings:
    return Settings()
