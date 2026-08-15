import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from docquery.api.auth import require_auth, roles_to_clearance, roles_to_sectors
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
from docquery.folders import normalize_segment
from docquery.generate.rag import query_pipeline
from docquery.ingest.pipeline import ingest_source
from docquery.ingest.sources import (
    SourceError,
    is_allowed_uri,
    source_scheme,
    validate_uri,
)

logger = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]

# /health stays open so the Docker healthcheck can reach it without a token.
system_router = APIRouter()
# Everything else requires a valid bearer token when auth_enabled is set.
router = APIRouter(dependencies=[Depends(require_auth)])


def get_user_clearance(
    settings: SettingsDep,
    claims: Annotated[dict | None, Depends(require_auth)],
    x_user_clearance: Annotated[int, Header()] = 0,
) -> int:
    """Resolve the caller's clearance level.

    With auth enabled it comes from the token's app roles and the
    X-User-Clearance header is ignored — otherwise any caller could raise their
    own clearance past what the token grants. With auth disabled the header
    remains the demo path, bound-checked against settings.max_clearance_level.
    """
    if settings.auth_enabled:
        clearance = roles_to_clearance((claims or {}).get("roles", []), settings)
        if clearance > 0:
            logger.info("Query authorized with clearance=%d", clearance)
        return clearance

    if not (0 <= x_user_clearance <= settings.max_clearance_level):
        raise HTTPException(
            status_code=400,
            detail=(
                f"X-User-Clearance must be between 0 and {settings.max_clearance_level}"
            ),
        )
    if x_user_clearance > 0:
        logger.info("Query authorized with clearance=%d", x_user_clearance)
    return x_user_clearance


ClearanceDep = Annotated[int, Depends(get_user_clearance)]


def get_user_sectors(
    settings: SettingsDep,
    claims: Annotated[dict | None, Depends(require_auth)],
    x_user_sectors: Annotated[str | None, Header()] = None,
) -> list[str] | None:
    """Resolve which sectors the caller may read.

    Three states, and the difference between the last two matters: None means
    "do not filter", while an empty list means "reads nothing". With auth on the
    token decides and the header is ignored, so a caller cannot widen its own
    reach; a token with no mapped role gets [] and sees nothing.

    With auth off there is no identity to trust, so the default is None — the
    header stays as the way to exercise the filter in demos and tests.
    """
    if settings.auth_enabled:
        sectors = roles_to_sectors((claims or {}).get("roles", []), settings)
        logger.info("Query authorized for sectors=%s", sectors or "none")
        return sectors

    if x_user_sectors is None:
        return None
    return sorted(
        {s for raw in x_user_sectors.split(",") if (s := normalize_segment(raw))}
    )


SectorsDep = Annotated[list[str] | None, Depends(get_user_sectors)]


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
        expired = [k for k, v in self._items.items() if now - v["created_at"] > ttl]
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


def _run_ingest(task_id: str, source: str, settings: Settings) -> None:
    _tasks.update(task_id, status="running")
    try:
        result = ingest_source(source, settings=settings)
        _tasks.update(task_id, status="done", **result)
    except Exception:
        logger.exception("Ingest task %s failed", task_id)
        _tasks.update(task_id, status="error", error="ingestion failed")


def _checked_remote_source(uri: str, settings: Settings) -> str:
    """Authorize a remote ingest URI, or raise 400.

    The allowlist is checked before anything else, so a caller cannot use the
    error message to learn which connectors this deployment has credentials for.
    It is empty by default: the endpoint pulls from no remote location until an
    operator names one, the remote counterpart of the ingest_root check.
    """
    if not is_allowed_uri(uri, settings.ingest_allowed_source_prefixes):
        raise HTTPException(
            status_code=400,
            detail="source URI is not under an allowed source prefix",
        )
    try:
        validate_uri(uri, settings)
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return uri


@system_router.get("/health", tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post("/query", tags=["query"])
def query(
    request: QueryRequest,
    settings: SettingsDep,
    user_clearance: ClearanceDep,
    sectors: SectorsDep,
) -> QueryResponse:
    blocked, reason = check_input(request.query)
    if blocked:
        raise HTTPException(status_code=400, detail=f"Query rejected: {reason}")
    result = query_pipeline(
        request.query,
        settings=settings,
        user_clearance=user_clearance,
        sectors=sectors,
        folders=request.folders,
        source=request.source,
        tags=request.tags,
    )
    return QueryResponse(**result)


@router.post("/ingest", tags=["ingest"], status_code=202)
def ingest(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    settings: SettingsDep,
) -> IngestJobResponse:
    if source_scheme(request.path) is not None:
        source = _checked_remote_source(request.path, settings)
    else:
        root = settings.ingest_root.resolve()
        try:
            path = Path(request.path).resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Path not found: {request.path}"
            )
        if not path.is_relative_to(root):
            raise HTTPException(
                status_code=400, detail="path must live under configured ingest_root"
            )
        source = str(path)

    task_id = str(uuid.uuid4())
    _tasks.create(task_id, settings)
    background_tasks.add_task(_run_ingest, task_id, source, settings)
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
