import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from docquery.api.guard import check_input
from docquery.api.schemas import (
    HealthResponse,
    IngestJobResponse,
    IngestRequest,
    IngestStatusResponse,
    QueryRequest,
    QueryResponse,
)
from docquery.config import Settings, get_settings
from docquery.generate.rag import query_pipeline
from docquery.ingest.pipeline import ingest_path

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_user_clearance(x_user_clearance: Annotated[int, Header()] = 0) -> int:
    """Read clearance level from the X-User-Clearance HTTP header (default 0).

    In a real system this would come from a verified JWT claim. Here it is an
    unauthenticated header to demonstrate RBAC filtering without adding an auth
    dependency outside the sprint scope.
    """
    return max(0, x_user_clearance)


ClearanceDep = Annotated[int, Depends(get_user_clearance)]

_tasks: dict[str, dict] = {}


def _run_ingest(task_id: str, path: Path, settings: Settings) -> None:
    _tasks[task_id]["status"] = "running"
    try:
        result = ingest_path(path, settings=settings)
        _tasks[task_id].update(status="done", **result)
    except Exception as exc:
        _tasks[task_id].update(status="error", error=str(exc))


@router.get("/health", tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post("/query", tags=["query"])
def query(
    request: QueryRequest,
    settings: SettingsDep,
    user_clearance: ClearanceDep,
) -> QueryResponse:
    blocked, reason = check_input(request.query)
    if blocked:
        raise HTTPException(status_code=400, detail=f"Query rejected: {reason}")
    result = query_pipeline(
        request.query, settings=settings, user_clearance=user_clearance
    )
    return QueryResponse(**result)


@router.post("/ingest", tags=["ingest"], status_code=202)
def ingest(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    settings: SettingsDep,
) -> IngestJobResponse:
    path = Path(request.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {request.path}")
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "status": "pending",
        "chunks": None,
        "deleted": None,
        "error": None,
    }
    background_tasks.add_task(_run_ingest, task_id, path, settings)
    return IngestJobResponse(task_id=task_id, status="pending")


@router.get("/ingest/{task_id}", tags=["ingest"])
def ingest_status(task_id: str) -> IngestStatusResponse:
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return IngestStatusResponse(task_id=task_id, **task)
