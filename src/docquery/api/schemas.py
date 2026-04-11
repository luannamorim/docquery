from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class QueryRequest(BaseModel):
    query: str


class Source(BaseModel):
    index: int
    source: str
    chunk_index: int
    score: float
    text: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[Source]
    query: str
    model: str
