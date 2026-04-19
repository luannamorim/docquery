from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, examples=["How does hybrid search work?"])


class Source(BaseModel):
    index: int = Field(description="1-based citation index, matches [N] in answer")
    source: str = Field(description="Source document path")
    chunk_index: int = Field(description="Chunk position within the source document")
    score: float = Field(description="Cross-encoder relevance score")
    text: str = Field(description="Retrieved passage text")


class QueryResponse(BaseModel):
    answer: str
    sources: list[Source]
    query: str
    model: str


class IngestRequest(BaseModel):
    path: str = Field(min_length=1, examples=["docs/sample"])


class IngestResponse(BaseModel):
    chunks: int
    deleted: int
    path: str
