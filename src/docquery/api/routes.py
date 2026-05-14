import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
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

logger = logging.getLogger(__name__)

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_user_clearance(
    x_user_clearance: Annotated[int, Header()] = 0,
    settings: SettingsDep = None,  # type: ignore[assignment]
) -> int:
    """Read clearance level from the X-User-Clearance HTTP header (default 0).

    In a real system this would come from a verified JWT claim. Here it is an
    unauthenticated header to demonstrate RBAC filtering without adding an auth
    dependency outside the sprint scope. Bound-checked against
    settings.max_clearance_level so callers cannot read above the configured
    ceiling.
    """
    if not (0 <= x_user_clearance <= settings.max_clearance_level):
        raise HTTPException(
            status_code=400,
            detail=(
                f"X-User-Clearance must be between 0 and "
                f"{settings.max_clearance_level}"
            ),
        )
    if x_user_clearance > 0:
        logger.info("Query authorized with clearance=%d", x_user_clearance)
    return x_user_clearance


ClearanceDep = Annotated[int, Depends(get_user_clearance)]


class _TaskStore:
    """In-process task store with TTL expiry and bounded size.

    Single-worker only. Production deployments with --workers > 1 must move
    this to an external store (Redis/Qdrant payload) — documented in SPEC.md
    as a production consideration.
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, dict] = OrderedDict()

    def _evict(self, settings: Settings) -> None:
        now = datetime.now(UTC)
        ttl = timedelta(seconds=settings.task_ttl_seconds)
        expired = [
            k for k, v in self._items.items() if now - v["created_at"] > ttl
        ]
        for k in expired:
            del self._items[k]
        while len(self._items) > settings.task_max_size:
            self._items.popitem(last=False)

    def create(self, task_id: str, settings: Settings) -> None:
        self._items[task_id] = {
            "status": "pending",
            "chunks": None,
            "deleted": None,
            "error": None,
            "created_at": datetime.now(UTC),
        }
        self._items.move_to_end(task_id)
        self._evict(settings)

    def update(self, task_id: str, **fields) -> None:
        if task_id in self._items:
            self._items[task_id].update(fields)

    def get(self, task_id: str, settings: Settings) -> dict | None:
        self._evict(settings)
        return self._items.get(task_id)


_tasks = _TaskStore()


def _run_ingest(task_id: str, path: Path, settings: Settings) -> None:
    _tasks.update(task_id, status="running")
    try:
        result = ingest_path(path, settings=settings)
        _tasks.update(task_id, status="done", **result)
    except Exception:
        logger.exception("Ingest task %s failed", task_id)
        _tasks.update(task_id, status="error", error="ingestion failed")


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
    root = settings.ingest_root.resolve()
    try:
        path = Path(request.path).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Path not found: {request.path}")
    if not path.is_relative_to(root):
        raise HTTPException(
            status_code=400, detail="path must live under configured ingest_root"
        )
    task_id = str(uuid.uuid4())
    _tasks.create(task_id, settings)
    background_tasks.add_task(_run_ingest, task_id, path, settings)
    return IngestJobResponse(task_id=task_id, status="pending")


@router.get("/ingest/{task_id}", tags=["ingest"])
def ingest_status(task_id: str, settings: SettingsDep) -> IngestStatusResponse:
    task = _tasks.get(task_id, settings)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return IngestStatusResponse(
        task_id=task_id,
        status=task["status"],
        chunks=task.get("chunks"),
        deleted=task.get("deleted"),
        error=task.get("error"),
    )
