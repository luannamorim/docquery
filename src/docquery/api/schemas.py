from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class QueryRequest(BaseModel):
    # Drive the Swagger "Try it out" body with clean examples so optional
    # filters don't show misleading "string"/["string"] placeholders. The
    # first example (query only) is what /docs prefills by default.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "How does hybrid search work?"},
                {
                    "query": "payment terms",
                    "doc_types": ["contract"],
                    "tags": ["supply"],
                },
            ]
        }
    )

    query: str = Field(min_length=1)
    doc_types: list[str] | None = Field(
        default=None,
        description="Restrict retrieval to these document types, e.g. ['contract']",
    )
    folders: list[str] | None = Field(
        default=None,
        description=(
            "Restrict retrieval to sources under any of these folder names, "
            "matched at any depth of the ingested tree (case-insensitive)"
        ),
    )
    source: str | None = Field(
        default=None,
        description="Restrict retrieval to a single source document path",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Restrict retrieval to chunks carrying any of these tags",
    )


class Source(BaseModel):
    index: int = Field(description="1-based citation index, matches [N] in answer")
    source: str = Field(description="Source document path")
    chunk_index: int = Field(description="Chunk position within the source document")
    score: float = Field(description="Cross-encoder relevance score")
    text: str = Field(description="Retrieved passage text")
    section: str = Field(
        default="",
        description="Nearest header/section breadcrumb, empty when not detected",
    )
    doc_type: str = Field(
        default="",
        description="Document type of the source (e.g. contract, policy)",
    )
    folders: list[str] = Field(
        default_factory=list,
        description="Folder segments of the source, relative to the ingested root",
    )


class QueryResponse(BaseModel):
    answer: str
    sources: list[Source]
    query: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class IngestRequest(BaseModel):
    # Named `path` for backwards compatibility; it also accepts a remote folder
    # URI, which must fall under ingest_allowed_source_prefixes.
    path: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Local path under ingest_root, or a remote folder URI: "
            "sharepoint://<host>/sites/<site>/<drive>[/<folder>] or "
            "gdrive://<folder id>"
        ),
        examples=[
            "docs/sample",
            "sharepoint://contoso.sharepoint.com/sites/Eng/Documents/policies",
            "gdrive://1AbCdEfGhIjKlMnOpQrS",
        ],
    )


class IngestResponse(BaseModel):
    chunks: int
    deleted: int
    path: str


class IngestJobResponse(BaseModel):
    task_id: str
    status: str


class IngestStatusResponse(BaseModel):
    task_id: str
    status: str
    chunks: int | None = None
    deleted: int | None = None
    error: str | None = None
