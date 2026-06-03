from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, examples=["How does hybrid search work?"])
    doc_types: list[str] | None = Field(
        default=None,
        description="Restrict retrieval to these document types (e.g. ['contract'])",
        examples=[["contract"]],
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


class QueryResponse(BaseModel):
    answer: str
    sources: list[Source]
    query: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class IngestRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096, examples=["docs/sample"])


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
