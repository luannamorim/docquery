import json
import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from openai import OpenAI

from docquery.api.auth import require_admin, require_auth, roles_to_sectors
from docquery.api.guard import check_input
from docquery.api.schemas import (
    ConversationListResponse,
    ConversationResponse,
    FeedbackListResponse,
    FeedbackReportResponse,
    FeedbackRequest,
    FeedbackResolveRequest,
    FrontendConfig,
    HealthResponse,
    IngestJobResponse,
    IngestRequest,
    IngestStatusResponse,
    QueryRequest,
    QueryResponse,
)
from docquery.config import Settings, get_settings
from docquery.feedback.store import FeedbackStore
from docquery.folders import normalize_segment
from docquery.generate.contextualize import contextualize
from docquery.generate.rag import query_pipeline, query_pipeline_stream
from docquery.history.store import ConversationStore
from docquery.ingest.pipeline import ingest_source
from docquery.ingest.sources import (
    SourceError,
    is_allowed_uri,
    source_scheme,
    validate_uri,
)
from docquery.retrieve.lookup import modified_for_sources, sector_for_source

logger = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]

# /health stays open so the Docker healthcheck can reach it without a token.
system_router = APIRouter()
# Everything else requires a valid bearer token when auth_enabled is set.
router = APIRouter(dependencies=[Depends(require_auth)])


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


@lru_cache
def _store_for(dsn: str) -> ConversationStore:
    """One store per DSN, schema applied once.

    Cached because ConversationStore holds no connection — it opens one per
    operation — so the only cost worth avoiding is re-running init_schema on
    every request.
    """
    store = ConversationStore(dsn)
    store.init_schema()
    return store


def get_store(settings: SettingsDep) -> ConversationStore | None:
    """The conversation store, or None when history is off.

    A dependency rather than a module global so tests can swap it through
    app.dependency_overrides — the same reason auth takes Settings as a
    parameter instead of calling get_settings().
    """
    if not settings.history_enabled:
        return None
    return _store_for(settings.history_dsn)


StoreDep = Annotated["ConversationStore | None", Depends(get_store)]


def get_owner(
    settings: SettingsDep,
    claims: Annotated[dict | None, Depends(require_auth)],
) -> str | None:
    """Who owns the conversations of this request: the token's object id.

    None means "no owner can be established", which disables history for the
    request rather than falling back to a shared bucket. history_enabled
    already requires auth_enabled, so this is the residual case of a token that
    carries no oid at all.
    """
    if not settings.history_enabled:
        return None
    return (claims or {}).get("oid") or None


OwnerDep = Annotated[str | None, Depends(get_owner)]


@lru_cache
def _feedback_store_for(dsn: str) -> FeedbackStore:
    """One store per DSN, schema applied once — see _store_for."""
    store = FeedbackStore(dsn)
    store.init_schema()
    return store


def get_feedback_store(settings: SettingsDep) -> FeedbackStore | None:
    """The feedback store, or None when the feature is off.

    A dependency for the same reason get_store is: tests swap it through
    app.dependency_overrides. Feedback shares history's database but not its
    switch — the two features toggle apart.
    """
    if not settings.feedback_enabled:
        return None
    return _feedback_store_for(settings.history_dsn)


FeedbackStoreDep = Annotated["FeedbackStore | None", Depends(get_feedback_store)]


def get_reporter(
    settings: SettingsDep,
    claims: Annotated[dict | None, Depends(require_auth)],
) -> str | None:
    """Who is flagging: the token's object id.

    Deliberately not get_owner, which is gated on history_enabled and would
    silently disable feedback whenever history is off. feedback_enabled
    already requires auth_enabled, so None is the residual case of a token
    with no oid at all.
    """
    if not settings.feedback_enabled:
        return None
    return (claims or {}).get("oid") or None


ReporterDep = Annotated[str | None, Depends(get_reporter)]


def get_reporter_name(
    settings: SettingsDep,
    claims: Annotated[dict | None, Depends(require_auth)],
) -> str:
    """The reporter's display name, for the review list.

    Entra puts it in `name` on v2 tokens, with `preferred_username` (the UPN)
    as the fallback. "" when the token carries neither — identity stays keyed
    on the oid; this is display only, snapshotted at report time.
    """
    if not settings.feedback_enabled:
        return ""
    claims = claims or {}
    return claims.get("name") or claims.get("preferred_username") or ""


ReporterNameDep = Annotated[str, Depends(get_reporter_name)]


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


@system_router.get("/config", tags=["system"])
def frontend_config(settings: SettingsDep) -> FrontendConfig:
    """Public identifiers the browser needs before it can obtain a token.

    On system_router, and that is the point: a client cannot present a bearer
    token until it knows which tenant and which client id to ask for one with.
    The second and last deliberate exception to "new endpoints go on `router`",
    alongside /health.

    Everything here is public by definition — tenant and application ids are
    identifiers, and the SPA is a public client with no secret to leak. Serving
    them rather than baking them into the bundle is what lets one image be
    configured per environment.
    """
    return FrontendConfig(
        tenantId=settings.azure_tenant_id,
        clientId=settings.frontend_client_id,
        apiClientId=settings.azure_client_id,
        appName=settings.app_name,
        feedbackEnabled=settings.feedback_enabled,
    )


def _redacted(text: str, settings: Settings) -> str:
    """PII redaction before history persistence, mirroring the ingest seam.

    Citations are payload-derived and already redacted at ingest; the question
    and the answer are the two strings that reach MySQL without passing
    through the pipeline, so they get the same treatment here.
    """
    if not settings.pii_redaction_enabled:
        return text
    from docquery.ingest.redact import redact_text

    return redact_text(text)


def _owned_turns(conversation_id: str, store, owner: str | None) -> list:
    """The caller's turns, or 404.

    404 and never 403: a 403 would confirm the id belongs to somebody, which is
    precisely what someone enumerating ids is after. History being off answers
    the same way, so probing cannot tell a disabled feature from a missing
    conversation either.
    """
    turns = (
        None
        if store is None or owner is None
        else store.turns(conversation_id, owner=owner)
    )
    if turns is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return turns


def _resolve_follow_up(
    query: str,
    conversation_id: str | None,
    store,
    owner: str | None,
    settings: Settings,
) -> tuple[str, bool]:
    """Return (query to retrieve with, whether it was rewritten).

    A first turn is never rewritten: no earlier question means nothing to
    resolve, the LLM is not called, and the pipeline runs exactly as it did
    before conversations existed — which is what keeps the eval baseline
    comparable.
    """
    if store is None or owner is None or not conversation_id:
        return query, False

    previous = store.previous_questions(
        conversation_id, owner=owner, limit=settings.history_context_turns
    )
    if not previous:
        return query, False

    openai_client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value() or None,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )
    try:
        resolved = contextualize(query, previous, settings, openai_client)
    except Exception:
        # A failed rewrite must not fail the question. Retrieving with the
        # original text gives a worse answer for a follow-up, not no answer.
        logger.warning("Follow-up rewrite failed; retrieving with the original query")
        return query, False
    return resolved, resolved != query


def _flag_reported(
    sources: list[dict], feedback: "FeedbackStore | None", sectors: list[str] | None
) -> None:
    """Mark each source that carries an open outdated report the caller may see.

    Existence only — comments and counts stay in GET /feedback. The caller's
    sectors bound the lookup the same way they bound every other feedback read,
    so a report in a compartment the token does not grant stays invisible.
    Answering questions must not depend on the feedback database: a failed
    lookup logs and the sources go out unflagged, never a 500.
    """
    if feedback is None or not sources:
        return
    try:
        open_reports = feedback.reported([s["source"] for s in sources], sectors)
    except Exception:
        logger.warning("Feedback lookup failed; sources go out unflagged")
        return
    for s in sources:
        s["flagged"] = s["source"] in open_reports


@router.post("/query", tags=["query"])
def query(
    request: QueryRequest,
    settings: SettingsDep,
    sectors: SectorsDep,
    store: StoreDep,
    owner: OwnerDep,
    feedback: FeedbackStoreDep,
) -> QueryResponse:
    blocked, reason = check_input(request.query)
    if blocked:
        raise HTTPException(status_code=400, detail=f"Query rejected: {reason}")

    history_on = store is not None and owner is not None
    conversation_id = request.conversation_id
    if history_on and conversation_id:
        # Resolving against a conversation the caller does not own must not
        # reveal that it exists, so an unknown id is refused exactly like
        # someone else's.
        if store.turns(conversation_id, owner=owner) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    elif not history_on:
        conversation_id = None

    retrieval_query, rewritten = _resolve_follow_up(
        request.query, conversation_id, store, owner, settings
    )
    if rewritten:
        # The rewrite is model output built from caller-supplied text, so it
        # goes through the same guard the original question did rather than
        # being trusted because we produced it.
        blocked, reason = check_input(retrieval_query)
        if blocked:
            raise HTTPException(status_code=400, detail=f"Query rejected: {reason}")

    result = query_pipeline(
        retrieval_query,
        settings=settings,
        sectors=sectors,
        folders=request.folders,
        source=request.source,
        tags=request.tags,
    )
    # The caller asked their question, not our rewrite of it.
    result["query"] = request.query
    _flag_reported(result["sources"], feedback, sectors)

    if history_on:
        if conversation_id is None:
            conversation_id = store.create(owner=owner)
        store.append(
            conversation_id,
            owner=owner,
            question=_redacted(request.query, settings),
            answer=_redacted(result["answer"], settings),
            rewritten_question=_redacted(retrieval_query, settings)
            if rewritten
            else "",
            citations=result["sources"],
            sectors=sectors or [],
            model=result["model"],
            tokens_in=result["tokens_in"],
            tokens_out=result["tokens_out"],
            cost_usd=result["cost_usd"],
        )

    return QueryResponse(
        **result,
        conversation_id=conversation_id,
        rewritten_query=retrieval_query if rewritten else None,
    )


#: Buffering is the failure mode that looks like success: a proxy that holds the
#: body delivers a complete, correct response with no error anywhere — the
#: stream simply stops being a stream. nginx and friends honour this.
#:
#: No Cache-Control here on purpose: SecurityHeadersMiddleware already sets
#: no-store on every response, which is stricter than the no-cache an SSE
#: endpoint would normally ask for, and setting it here would just be overridden.
_SSE_HEADERS = {"X-Accel-Buffering": "no"}


def _sse(event: str, payload: dict) -> str:
    # separators without spaces, and no newlines inside data: a raw newline
    # would terminate the SSE frame early and split one event into two.
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/query/stream", tags=["query"])
def query_stream(
    request: QueryRequest,
    settings: SettingsDep,
    sectors: SectorsDep,
    store: StoreDep,
    owner: OwnerDep,
    feedback: FeedbackStoreDep,
) -> StreamingResponse:
    """The same answer as POST /query, delivered as it is produced.

    POST rather than GET even though EventSource only speaks GET: a GET puts the
    question in the URL, and from there into access logs, proxy logs and browser
    history. rag.py logs only a hash of the query precisely to avoid that, and a
    streaming endpoint must not undo it. Clients read this with fetch() and a
    ReadableStream instead.
    """
    blocked, reason = check_input(request.query)
    if blocked:
        raise HTTPException(status_code=400, detail=f"Query rejected: {reason}")

    history_on = store is not None and owner is not None
    conversation_id = request.conversation_id
    if history_on and conversation_id:
        if store.turns(conversation_id, owner=owner) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    elif not history_on:
        conversation_id = None

    retrieval_query, rewritten = _resolve_follow_up(
        request.query, conversation_id, store, owner, settings
    )
    if rewritten:
        blocked, reason = check_input(retrieval_query)
        if blocked:
            raise HTTPException(status_code=400, detail=f"Query rejected: {reason}")

    def events():
        nonlocal conversation_id
        answer = ""
        final: dict = {}
        # Kept from the event that carried them. The closing event has no
        # sources — they were sent before the first token, which is the whole
        # design — so recording the turn from `final` stored an empty citation
        # list and history rendered [1] markers pointing at nothing.
        sources: list = []
        try:
            for event in query_pipeline_stream(
                retrieval_query,
                settings=settings,
                sectors=sectors,
                folders=request.folders,
                source=request.source,
                tags=request.tags,
            ):
                kind = event["type"]
                if kind == "sources":
                    sources = event["sources"]
                    _flag_reported(sources, feedback, sectors)
                    yield _sse("sources", {"sources": sources})
                elif kind == "token":
                    answer += event["text"]
                    yield _sse("token", {"t": event["text"]})
                else:
                    final = event
        except Exception:
            # The status line is long gone by now, so an error cannot be a 500 —
            # it has to travel as an event. The detail is deliberately generic,
            # matching how the rest of the API answers.
            logger.exception("Streaming query failed")
            yield _sse("error", {"detail": "Query failed"})
            return
        finally:
            # Runs on client disconnect too, where the generator is closed
            # mid-stream. The partial answer was already delivered to the user,
            # so the audit trail records what they saw rather than nothing —
            # `complete` says which of the two happened.
            if history_on and answer:
                if conversation_id is None:
                    conversation_id = store.create(owner=owner)
                store.append(
                    conversation_id,
                    owner=owner,
                    question=_redacted(request.query, settings),
                    answer=_redacted(answer, settings),
                    rewritten_question=_redacted(retrieval_query, settings)
                    if rewritten
                    else "",
                    citations=sources,
                    sectors=sectors or [],
                    model=final.get("model", settings.llm_model),
                    tokens_in=final.get("tokens_in", 0),
                    tokens_out=final.get("tokens_out", 0),
                    cost_usd=final.get("cost_usd", 0.0),
                    complete=bool(final),
                )

        yield _sse(
            "done",
            {
                "conversation_id": conversation_id,
                "query": request.query,
                "rewritten_query": retrieval_query if rewritten else None,
                "model": final.get("model", settings.llm_model),
                "tokens_in": final.get("tokens_in", 0),
                "tokens_out": final.get("tokens_out", 0),
                "cost_usd": final.get("cost_usd", 0.0),
            },
        )

    return StreamingResponse(
        events(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/conversations", tags=["history"])
def list_conversations(
    store: StoreDep,
    owner: OwnerDep,
) -> ConversationListResponse:
    """The caller's own conversations, most recent first.

    Registered before /conversations/{conversation_id} so the literal path is
    matched first — Starlette resolves in declaration order, and the parameter
    route would otherwise capture "conversations" as an id.
    """
    if store is None or owner is None:
        return ConversationListResponse(conversations=[])
    return ConversationListResponse(conversations=store.list_conversations(owner=owner))


@router.get("/conversations/{conversation_id}", tags=["history"])
def get_conversation(
    conversation_id: str,
    store: StoreDep,
    owner: OwnerDep,
) -> ConversationResponse:
    turns = _owned_turns(conversation_id, store, owner)
    return ConversationResponse(conversation_id=conversation_id, turns=turns)


@router.delete("/conversations/{conversation_id}", tags=["history"], status_code=204)
def delete_conversation(
    conversation_id: str,
    store: StoreDep,
    owner: OwnerDep,
) -> Response:
    """Erase a conversation. Available regardless of the retention policy.

    Retention is unbounded by configuration, but the right to erasure belongs to
    the data subject and does not depend on it — without this route there is no
    way to answer such a request.
    """
    if store is None or owner is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not store.delete(conversation_id, owner=owner):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return Response(status_code=204)


@router.post("/feedback", tags=["feedback"], status_code=201)
def report_document(
    request: FeedbackRequest,
    settings: SettingsDep,
    sectors: SectorsDep,
    feedback: FeedbackStoreDep,
    reporter: ReporterDep,
    reporter_name: ReporterNameDep,
    response: Response,
) -> FeedbackReportResponse:
    """Flag a document as outdated. A record for review, nothing more: it does
    not change retrieval, warn other askers or trigger a re-ingest.

    404 for the feature being off, a token with no oid or no sectors, an
    unknown source and a source outside the caller's sectors alike — flagging
    must not confirm that a document exists. The sector is derived here from
    the index, never taken from the client: a caller-supplied sector could
    file a report into a compartment its token does not grant.

    The comment is not LLM-bound, so check_input deliberately does not run on
    it: the schema caps its length and the SPA renders it as text, never HTML.
    """
    if feedback is None or reporter is None or sectors == []:
        raise HTTPException(status_code=404, detail="Document not found")
    sector = sector_for_source(request.source, settings, sectors)
    if sector is None:
        raise HTTPException(status_code=404, detail="Document not found")
    created = feedback.report(
        request.source, sector, reporter, request.comment, reporter_name
    )
    if not created:
        # A repeat flag by the same caller updated the existing report.
        response.status_code = 200
    return FeedbackReportResponse(source=request.source, sector=sector, created=created)


@router.get("/feedback", tags=["feedback"])
def list_reported_documents(
    settings: SettingsDep,
    sectors: SectorsDep,
    feedback: FeedbackStoreDep,
) -> FeedbackListResponse:
    """The reported documents in the caller's sectors, newest activity first.

    Empty rather than 404 when the feature is off, mirroring GET
    /conversations with history off. The store handles the sectors
    three-state; None still means "do not filter" even though auth being
    required makes it unreachable here today.

    Each document carries its update date, read live from the index so a
    re-ingest after the flag shows here. The list must not depend on Qdrant:
    a failed lookup logs and the documents go out dateless, never a 500.
    """
    if feedback is None:
        return FeedbackListResponse(documents=[])
    documents = feedback.list_reports(sectors)
    try:
        modified = modified_for_sources(
            [d["source"] for d in documents], settings, sectors
        )
    except Exception:
        logger.warning("Modified-at lookup failed; the review list goes out dateless")
        modified = {}
    for d in documents:
        d["modified_at"] = modified.get(d["source"], "")
    return FeedbackListResponse(documents=documents)


@router.post("/feedback/resolve", tags=["feedback"], status_code=204)
def resolve_report(
    request: FeedbackResolveRequest,
    sectors: SectorsDep,
    feedback: FeedbackStoreDep,
) -> Response:
    """Erase every report for a document — it was reviewed, or the flag was
    wrong. Any member of the document's sector may resolve; the sector
    predicate lives in the store's WHERE clause, so resolving from outside the
    sector is indistinguishable from resolving a document nobody reported.
    """
    if feedback is None or not feedback.resolve(request.source, sectors):
        raise HTTPException(status_code=404, detail="Document not found")
    return Response(status_code=204)


@router.post(
    "/ingest",
    tags=["ingest"],
    status_code=202,
    dependencies=[Depends(require_admin)],
)
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


@router.get("/ingest/{task_id}", tags=["ingest"], dependencies=[Depends(require_admin)])
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
