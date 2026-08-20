from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class FrontendConfig(BaseModel):
    """Public Entra identifiers for the browser client.

    camelCase because it is consumed directly by TypeScript and nothing else;
    renaming it on the client would only add a mapping layer to maintain.
    """

    tenantId: str
    clientId: str
    apiClientId: str
    appName: str = "docquery"
    # Whether the outdated-document flag is available. A boolean feature
    # switch is public by the same argument as appName: it reveals nothing a
    # caller could not learn by trying the endpoint.
    feedbackEnabled: bool = False


class QueryRequest(BaseModel):
    # Drive the Swagger "Try it out" body with clean examples so optional
    # filters don't show misleading "string"/["string"] placeholders. The
    # first example (query only) is what /docs prefills by default.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "How does hybrid search work?"},
                {
                    "query": "payment terms",
                    "folders": ["contratos"],
                    "tags": ["supply"],
                },
            ]
        }
    )

    query: str = Field(min_length=1)
    conversation_id: str | None = Field(
        default=None,
        max_length=36,
        description=(
            "Continue an earlier conversation: the question is resolved against "
            "the questions already asked in it. Omit it to start a new one — the "
            "id to continue with comes back in the response"
        ),
    )
    folders: list[str] | None = Field(
        default=None,
        description=(
            "Restrict retrieval to sources under any of these folder names, "
            "matched at any depth of the ingested tree (case-insensitive)"
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Restrict retrieval to one document, matched exactly: its local path "
            "or remote URI, copied from the 'source' of a citation"
        ),
    )
    tags: list[str] | None = Field(
        default=None,
        description="Restrict retrieval to chunks carrying any of these tags",
    )


class Source(BaseModel):
    index: int = Field(description="1-based citation index, matches [N] in answer")
    source: str = Field(
        description="Local path or remote URI identifying the document; "
        "pass it back as the request's 'source' to scope a follow-up query"
    )
    chunk_index: int = Field(description="Chunk position within the source document")
    score: float = Field(description="Cross-encoder relevance score")
    text: str = Field(description="Retrieved passage text")
    section: str = Field(
        default="",
        description="Nearest header/section breadcrumb, empty when not detected",
    )
    folders: list[str] = Field(
        default_factory=list,
        description="Folder segments of the source, relative to the ingested root",
    )
    modified_at: str = Field(
        default="",
        description=(
            "When the document was last updated (UTC, RFC 3339), from the "
            "library it lives in or the metadata inside the file. Empty when "
            "neither records one — never the ingest or download time"
        ),
    )
    flagged: bool = Field(
        default=False,
        description=(
            "True when an open outdated-document report exists for this source "
            "within the caller's sectors. Existence only — comments and counts "
            "live in GET /feedback. Always false when feedback is disabled"
        ),
    )


class QueryResponse(BaseModel):
    answer: str
    sources: list[Source]
    query: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    conversation_id: str | None = Field(
        default=None,
        description=(
            "Pass this back to ask a follow-up. None when history is disabled"
        ),
    )
    rewritten_query: str | None = Field(
        default=None,
        description=(
            "What actually went to retrieval, when the question was resolved "
            "against earlier turns. None on a first turn, which is not rewritten"
        ),
    )


class Turn(BaseModel):
    seq: int = Field(description="1-based position in the conversation")
    question: str
    answer: str
    rewritten_question: str = ""
    citations: list[Source] = Field(default_factory=list)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    complete: bool = Field(
        default=True,
        description=(
            "False when the client stopped receiving a streamed answer part-way"
        ),
    )
    created_at: datetime


class ConversationResponse(BaseModel):
    conversation_id: str
    turns: list[Turn]


class ConversationSummary(BaseModel):
    id: str
    title: str = Field(
        default="",
        description="The opening question — what the user will recognise it by",
    )
    created_at: datetime
    last_turn_at: datetime | None = None


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]


class FeedbackRequest(BaseModel):
    source: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "The document to flag as outdated: its local path or remote URI, "
            "copied from the 'source' of a citation"
        ),
    )
    comment: str = Field(
        default="",
        max_length=500,
        description="Optional note for whoever reviews the document",
    )


class FeedbackReportResponse(BaseModel):
    source: str
    sector: str
    created: bool = Field(
        description="False when this caller had already flagged the document — "
        "the report was updated, not duplicated"
    )


class ReportComment(BaseModel):
    comment: str
    reported_at: datetime


class ReportedDocument(BaseModel):
    source: str
    sector: str
    report_count: int
    last_reported_at: datetime
    comments: list[ReportComment] = Field(default_factory=list)


class FeedbackListResponse(BaseModel):
    documents: list[ReportedDocument]


class FeedbackResolveRequest(BaseModel):
    # POST with a body rather than DELETE with a query parameter: a source is
    # an arbitrary path or URI, and a query string lands in access and proxy
    # logs — the same leak /query/stream avoids by never being GET.
    source: str = Field(min_length=1, max_length=4096)


class IngestRequest(BaseModel):
    # Named `path` for backwards compatibility; it also accepts a remote folder
    # URI, which must fall under ingest_allowed_source_prefixes.
    path: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Local path under ingest_root, or a remote folder URI: "
            "sharepoint://<host>/sites/<site>/<drive>[/<folder>] or "
            "gdrive://<folder id>"
        ),
        examples=[
            "docs/sample",
            "sharepoint://contoso.sharepoint.com/sites/Eng/Documents/policies",
            "gdrive://1AbCdEfGhIjKlMnOpQrS",
        ],
    )


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
