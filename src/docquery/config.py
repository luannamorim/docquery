from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
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

    # Docling parsing
    # docling_enabled gates the whole Docling path: when False, load_document
    # dispatches through LOADERS exactly as before and the docling package is
    # never imported. .txt and .md always stay on the legacy path — Docling has
    # no plain-text backend, and the markdown path carries frontmatter parsing
    # and heading promotion that must not regress.
    docling_enabled: bool = False
    # OCR runs only over bitmap regions, so native-text PDFs pay no extra cost.
    docling_ocr_enabled: bool = True
    # RapidOCR (bundled with docling, torch backend) uses a single language per
    # run; extra values are ignored. "en" selects the PP-OCRv6 recognizer, whose
    # character set also covers Portuguese diacritics, so one model serves both
    # languages. It is also the recognizer the Docker image prefetches — a
    # script-family value such as "latin" resolves to a different (PP-OCRv4)
    # checkpoint that is not in the image and would fail offline.
    docling_ocr_langs: list[str] = ["en"]
    # TableFormer structure recovery. CPU-heavy — disable for text-only corpora.
    docling_table_structure: bool = True
    # Conversion limits. Exceeding any of these fails the document with a clear
    # error instead of exhausting memory on a hostile or oversized input.
    docling_max_file_mb: int = 50
    docling_max_pages: int = 200
    docling_timeout_seconds: float = 300.0
    # Local model weights, set in the Docker image so conversion never reaches
    # the network at runtime. Also read natively by docling from the same env var.
    docling_artifacts_path: Path | None = None

    # Heading promotion for non-markdown procedural docs.
    # Patterns that match at line start are rewritten as "## ..." so the
    # markdown pipeline can extract them as sections.
    heading_patterns: list[str] = [
        r"^Passo \d+[:.]",
        r"^Step \d+[:.]",
    ]

    # Context expansion — fetch N neighbor chunks on each side of each reranked result
    context_expansion_window: int = 1

    # Ingest hardening — only paths under this root are accepted by /ingest;
    # symlinks pointing outside the root are filtered.
    ingest_root: Path = Path("docs")

    # Remote ingest sources (sharepoint:// and gdrive:// URIs)
    # Allowlist for /ingest, the counterpart of ingest_root for remote URIs. An
    # empty list is fail-closed: the API accepts no remote URI at all, so a
    # caller cannot make the server pull arbitrary sites from the tenant. The
    # CLI is unrestricted — it is already an operator-level entry point.
    ingest_allowed_source_prefixes: list[str] = []
    # Per-file download ceiling. Files above it are skipped, not fatal.
    source_max_file_mb: int = 50
    # SharePoint via Microsoft Graph, client credentials. Separate from the API's
    # own Entra ID app registration: this one is a confidential client that reads
    # documents, that one only validates callers' tokens.
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    sharepoint_client_secret: SecretStr | None = None
    # Google Drive service account. A path to the JSON key rather than the JSON
    # itself, so the credential is mounted as a file and never sits in the
    # environment where any subprocess could read it.
    gdrive_service_account_file: Path | None = None

    # Clearance / RBAC
    # default_clearance_level: applied to documents that don't match
    # clearance_policy. Default 0 (public) keeps the demo corpus accessible;
    # production deployments should set this above max_clearance_level
    # (fail-closed) and rely on clearance_policy for explicit access grants.
    default_clearance_level: int = 0
    max_clearance_level: int = 10
    # Path-prefix → clearance mapping applied at ingest time. The first
    # matching prefix wins; falls back to default_clearance_level on no match.
    clearance_policy: list[tuple[str, int]] = []

    # Auth (Azure Entra ID)
    # Opt-in like docling_enabled: the demo corpus and quickstart run without a
    # tenant. When False the API derives clearance from the X-User-Clearance
    # header exactly as before; production deployments must set AUTH_ENABLED=true.
    auth_enabled: bool = False
    # Not secrets — tenant and client (application) ID are public identifiers.
    # There is no client secret: the API validates tokens, it never requests them.
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    # App role → clearance level. JSON in .env: [["clearance.5", 5]]. The highest
    # level among the token's roles wins; tokens with no mapped role fall back to
    # default_clearance_level.
    auth_role_clearance_map: list[tuple[str, int]] = []
    # App role → sector (the top-level folder it may read). JSON in .env:
    # [["sector.rh", "rh"]]. A token's sectors are the union of its mapped
    # roles; a token with no mapped role reads nothing. Folders everyone may
    # read are an ordinary sector whose role is granted to every employee.
    auth_role_sector_map: list[tuple[str, str]] = []
    # Clock skew tolerance for exp/nbf/iat. Without it, container clock drift
    # produces intermittent 401s that are hard to diagnose.
    auth_leeway_seconds: int = 60

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

    @field_validator(
        "docling_artifacts_path", "gdrive_service_account_file", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty env var as "not configured" for optional paths.

        Path("") is Path("."), which is truthy, so the guards that check these
        fields would pass and the failure would surface much later as an
        IsADirectoryError on ".". Empty strings for str and SecretStr fields are
        already falsy, so only paths need this.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_auth_config(self) -> "Settings":
        """Fail fast when auth is on but unconfigured.

        Without this the API would boot with auth_enabled=True and no tenant,
        rejecting every token — or worse, appear protected while validating
        against an empty issuer.
        """
        if self.auth_enabled and not (self.azure_tenant_id and self.azure_client_id):
            raise ValueError(
                "auth_enabled requires azure_tenant_id and azure_client_id"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
