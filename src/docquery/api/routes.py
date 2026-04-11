from typing import Annotated

from fastapi import APIRouter, Depends

from docquery.api.schemas import HealthResponse, QueryRequest, QueryResponse
from docquery.config import Settings, get_settings
from docquery.generate.rag import query_pipeline

router = APIRouter(tags=["system"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post("/query")
def query(request: QueryRequest, settings: SettingsDep) -> QueryResponse:
    result = query_pipeline(request.query, settings=settings)
    return QueryResponse(**result)
