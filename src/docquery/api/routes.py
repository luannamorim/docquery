from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from docquery.api.schemas import (
    HealthResponse,
    IngestRequest,
    IngestResponse,
    QueryRequest,
    QueryResponse,
)
from docquery.config import Settings, get_settings
from docquery.generate.rag import query_pipeline
from docquery.ingest.pipeline import ingest_path

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/health", tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post("/query", tags=["query"])
def query(request: QueryRequest, settings: SettingsDep) -> QueryResponse:
    result = query_pipeline(request.query, settings=settings)
    return QueryResponse(**result)


@router.post("/ingest", tags=["ingest"], status_code=201)
def ingest(request: IngestRequest, settings: SettingsDep) -> IngestResponse:
    path = Path(request.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {request.path}")
    chunks = ingest_path(path, settings=settings)
    return IngestResponse(chunks=chunks, path=str(path))
