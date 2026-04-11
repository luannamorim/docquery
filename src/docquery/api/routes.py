from fastapi import APIRouter

from docquery.api.schemas import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")
