# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

docquery is a production-grade RAG (Retrieval-Augmented Generation) system for querying technical documentation. It returns answers with citations and confidence scores, evaluated with RAGAS metrics. It ships a small browser client (TypeScript + Vite, no framework) served by the API itself, and streams answers over SSE — both listed under *What NOT to Build* in `SPEC.md` and both deliberately reopened, like Docling and Auth before them. Authentication is Azure Entra ID bearer-token validation, opt-in via `AUTH_ENABLED`. Conversation history (multi-turn follow-ups plus an audit trail) is opt-in via `HISTORY_ENABLED` and backed by MySQL; `SPEC.md` still lists it under *What NOT to Build*, deliberately reopened like Docling and Auth before it.

## Commands

```bash
# Development
make serve                  # Start FastAPI server
make ingest docs/sample/    # Index documents into Qdrant
make eval                   # Run RAGAS evaluation

# Infrastructure
docker compose up           # Start app + Qdrant

# Testing
pytest                      # Run all tests
pytest tests/test_api.py    # Run a single test file
```

## Architecture

Three independent pipelines:

- **Ingestion** — Source (local path, `sharepoint://`, `gdrive://`) → Document Loader → Chunker (semantic + fixed-size fallback) → Embedder → Qdrant storage
- **Query** — Query Embedding → Hybrid Retrieval (dense + BM25 via Qdrant) → Cross-encoder Reranking → Context Assembly → LLM Generation → Response with citations
- **Evaluation** — RAGAS metrics (faithfulness, relevancy, context precision) with before/after comparison

Source layout under `src/docquery/`: `config.py`, `ingest/` (loader, sources, chunker, pipeline), `retrieve/` (embedder, hybrid, reranker), `generate/` (rag, contextualize), `api/` (app, routes, schemas, auth), `history/` (store, schema.sql). Eval lives in `eval/`.

### Frontend conventions

- **The API serves the SPA at `/`** (`mount_frontend` in `app.py`). Same origin is what keeps CORS out of this codebase — `app.py` still has none, deliberately. The mount is registered **last** (a `/` mount is a catch-all) and **conditionally** (a checkout has no build; tests and eval import the app without running npm).
- **`GET /config` is the second and last open endpoint**, alongside `/health`. The browser needs the tenant and client ids *before* it can obtain a token, so it cannot require one. It carries only public identifiers.
- **Streaming is POST, never GET.** `EventSource` only speaks GET, and a GET would put the question into access logs, proxy logs and browser history — undoing the care `rag.py` takes to log only a hash of the query. Clients parse SSE frames from `fetch` + `ReadableStream` instead.
- Tokens live in MSAL's **memory** cache, not `localStorage`: a token in `localStorage` outlives the tab and is readable by any script on the origin.
- No webfonts. This runs behind corporate TLS inspection that already blocks CDNs (see the HuggingFace note in `docker-compose.yml`); a font that fails to load is a design that fails to load.
- Sector colour is **derived by hashing the folder name** (`sectorColor` in `ui.ts`), never configured — the same rule `folders.py` follows. A new folder gets a colour on first appearance.

### Operating traps that have already cost time

- **`docker compose restart` does not re-read `.env`.** It restarts the process with the environment the container was created with, so a new variable never arrives. Use `docker compose up -d` (which recreates) — and remember that recreating reverts to the **image**, discarding anything copied in with `docker cp`.
- **A document that yields no chunks used to vanish silently.** A scanned PDF with no text layer produced zero chunks, the ingest reported success, and retrieval then answered questions about it with a confident "there is no such document". `warn_about_empty_documents` now names the file and points at `DOCLING_ENABLED`. Scanned corpora need `DOCLING_ENABLED=true`.
- **Re-ingesting empties the corpus while it runs.** `_ingest_documents` deletes a source's chunks before inserting the new ones, so a query during that window gets "no documents matched". With OCR the window is minutes, not seconds.
- **The rate limit keys on the socket address**, which behind Docker or a reverse proxy is the same for everybody — one bucket for the whole deployment. Set `RATE_LIMIT_TRUST_FORWARDED_FOR=true` wherever a proxy you control sets the header, and never where one does not.
- **`configure_logging` sets `propagate = False` on the `docquery` logger**, and the FastAPI lifespan calls it — so any test building a `TestClient` breaks `caplog` for everything after it. `tests/conftest.py` restores the logger around every test; keep that fixture.

### Answer language

- The model is told to **answer in the language of the question** by default (`system_prompt` in `rag.py`), not the language of the retrieved passages — an English contract read by a Portuguese speaker should answer in Portuguese. `ANSWER_LANGUAGE` fixes it instead when a deployment wants one language regardless.
- The three refusals in `_REFUSALS` never reach the model, so nothing else can translate them; they are keyed by language and fall back to English rather than raising. **The `no_match` wording must stay ambiguous between "nothing indexed" and "you cannot reach it" in every translation** — that ambiguity is the compartment guarantee, and translating it is exactly where it would be lost.

### Conversation history conventions

- **A first turn is never rewritten.** No `conversation_id` means no earlier question, so `contextualize` returns the query untouched *without calling the LLM*. That is what keeps `eval/dataset.json` comparable against the old baseline — the stateless path is byte-for-byte the path it always was.
- **The rewriter sees questions, never answers.** Answers carry passages lifted from indexed documents; feeding them back into a prompt would make every ingested file a potential instruction. `check_input` also re-runs on the rewritten query, because it is model output built from caller text.
- **Ownership is a `WHERE` clause, not a Python check.** Every store method takes the owner, so no path loads a conversation and decides afterwards. A conversation belonging to someone else is indistinguishable from one that does not exist — which is why the endpoints answer **404, never 403**: a 403 confirms the id to whoever is enumerating.
- `history_enabled` requires `auth_enabled` (validated in `config.py`): a conversation is owned by the token's `oid`, and without one it has no owner.
- Retention is unbounded by design; `DELETE /conversations/{id}` exists regardless, because the right to erasure does not depend on the retention policy.
- Store tests are opt-in against a real MySQL (`-m mysql`, `DOCQUERY_MYSQL_TEST_DSN`), mirroring the Docling integration split. The endpoint tests swap the store through `app.dependency_overrides`, so CI needs no database.

### Document feedback conventions

- **A report is a record for review, nothing more.** Flagging a document as outdated (`POST /feedback`) does not change retrieval, does not warn other askers and does not trigger a re-ingest — it exists so a person reviews the document at its source. Reports live in MySQL (`src/docquery/feedback/`), never in the Qdrant payload: re-ingest deletes a source's chunks, and a report must outlive the re-ingest it is asking for.
- **The sector is snapshotted server-side, never taken from the client.** `sector_for_source` (`retrieve/lookup.py`) reads it from the index *with the caller's sectors in the Qdrant filter*, so a document outside the caller's compartments answers 404 — the same "does not exist / cannot be reached" ambiguity the endpoints and the `no_match` refusal preserve. A caller-supplied sector could file a report into a compartment its token does not grant.
- **The review list is read by sector, not by owner.** Any member of the document's sector sees and resolves its reports — reviewing is a team act, unlike a conversation. The predicate is still a `WHERE` clause in the store, and the endpoints still answer **404, never 403**.
- `feedback_enabled` requires `auth_enabled` (a report is deduplicated by the token's `oid`) and `history_dsn` — it shares history's MySQL but not `history_enabled`; the two features toggle apart. `get_owner` is history-gated and must not be reused: feedback has its own `get_reporter`.
- **Resolve is POST with a body, not DELETE with a query parameter** (`POST /feedback/resolve`): a `source` is an arbitrary path or URI, and a query string lands in access and proxy logs — the same leak `/query/stream` avoids by never being GET.
- In the SPA the flag button is a **sibling** of the source card inside `.source-row`, never a child: the card is a `<button>` and a button inside a button is invalid HTML. Anything hiding a card must hide the row (`markCitedSources`), or the flag floats next to an empty slot.

### Ingest conventions

- **A document's `source` is its identity.** Deduplication and orphan pruning both match on it by prefix. Remote documents are indexed under their URI, never the temporary path they were downloaded to.
- Prefix matching must be bounded by a separator (`orphan_prefix_for`, `is_allowed_uri`) — an unterminated prefix silently captures sibling folders.
- **`folders` is derived, never configured** (`folders.py`, applied in `ingest_path`/`ingest_source`): the path segments relative to the root that was ingested. Both entry points derive it because only they know that root — inside `_ingest_documents` the `source` is already an opaque local path or URI. Like `sector` it gates retrieval scope, so it is rejected from frontmatter.
- **`sector` is the access boundary and is deliberately not `folders`.** It is the top-level segment alone (`sector_of`), matched exactly, because `folders` matches at any depth — reusing it would let `financeiro/rh/folha.pdf` grant the RH compartment. Both come from one `_place_document` call so they cannot drift apart.
- **`modified_at` is when the document was updated, and the filesystem never answers that.** mtime records the last write to *this copy* — in a corpus that arrives by sync, `docker cp` or checkout it is the copy date, which is why `docs/` here holds files sharing one mtime to the sub-second. The date comes from the library that recorded the edit (`lastModifiedDateTime`, `modifiedTime` — the latter is outside Drive's default projection and must be requested) or, failing that, from the timestamp the editor wrote inside the file (`ingest/modified.py`: PDF document information, OOXML core properties). Neither knows, the field stays empty, and the UI says "data desconhecida" — because "unknown" and "updated today" are opposite answers and the ingest date is the one thing always available.
- **Emphasis is derived terms: lexical index and metadata only — the passage never changes** (`ingest/emphasis.py`, flag `EMPHASIS_EXTRACTION_ENABLED`). Yellow/green PDF highlights join `_lexical` the way `document_terms` does and land in the `emphasis` payload field; `reranker._point_to_context` deliberately does not read them. Page-mapped on the Docling path (`page_number`), doc-level on the legacy path (load_pdf joins pages first). Heading promotion from CAPS yellow lines fires on the legacy path only — on Docling, `dl_doc` drives chunking and rewriting `content` is inert.
- **Red boxes inside screenshots are the raster half of emphasis** (`ingest/image_emphasis.py`, flag `IMAGE_EMPHASIS_ENABLED`). Embedded images come from pypdf (`page.images`, full fidelity), the OCR engine is built by Docling's own `RapidOcrModel` so model resolution can never diverge from page-level OCR, and results land in `emphasis_screen` — same INV-1 rule, same redaction coverage. Arrows/numbering stay out of scope (VLM territory).
- **PII redaction lives in `ingest_chunks`, the only door to Qdrant** (`ingest/redact.py`, flag `PII_REDACTION_ENABLED`). Chunk level, not document level, because the Docling path chunks from `dl_doc` and never reads `Document.content` — redacting the parsed document would silently miss every Docling-parsed file. Replacement into typed placeholders (`[CPF]`…), never removal; every detector validates (check digits, phone shape) because a 9-12 digit contract number is not a CPF. History gets the same treatment in `routes._redacted`. Enabling the flag changes chunk text and therefore point IDs: re-ingest.
- `sources.py` dispatches by URI scheme through a dict of functions, mirroring `LOADERS` in `loader.py`. Adding a connector means adding a fetcher and a validator, not a class hierarchy.
- Remote fetch tests drive `httpx.MockTransport` rather than patching internals, so pagination, streaming and the size cap are actually exercised.

### Retrieval conventions

- **The document a question names is a ranking signal, not a filter** (`retrieve/affinity.py`). `document_terms` puts the file name into the sparse vector, so retrieval finds a contract's chunks even though only 19% of them repeat the party name — but the cross-encoder scores `payload["text"]` alone, so the signal survives RRF and **dies at the rerank**. `named_sources` reads it back from the same terms and `rerank(prefer_sources=...)` gives those passages the slots first. A preference, never a `Filter`: naming a document does not claim no other document is relevant.
- **A term every candidate shares names nothing.** `contracts` carried by the whole candidate set separates none of it, so it is dropped before matching — otherwise "quais contratos temos" would reorder the list on a signal with no information in it. Terms under 3 characters are dropped too: a file named `contrato_de_prestacao.pdf` offers `de`, which appears in almost every Portuguese question.
- **`rerank` scores every candidate and truncates last.** The cut has to happen *after* the preference: with `RERANKER_TOP_K=5` over 20 candidates the named document's clause can rank 9th, and promoting inside an already truncated list would never see it. This is behaviour-preserving when nothing is named — `ranked` is score-sorted, so everything above `reranker_score_threshold` is a prefix and the filter cannot punch a hole for a lower-scoring passage to backfill. That equality is what keeps `eval/dataset.json` comparable.
- **Affinity and decomposition fix different halves and both are needed.** Measured on the real corpus (`make compare-document-scope`, results in `eval/results/document_scope/`): source precision on the named document went 45% → 55% with decomposition alone, → 100% with affinity alone. But affinity alone spends all 5 slots on the right contract and still misses the prazo clause, because one cross-encoder pass over "prazo **e** valor" rates every candidate mediocre. Only both together answer the question — affinity buys correctness, decomposition buys recall.
- Every tokenizer for retrieval goes through `sparse.tokens`. Query side and index side must agree exactly or a term the index holds is not the term the query asks for. Tokens are **accent-folded** (NFKD, combining marks dropped), so `quitação` is one token `quitacao` and `política` names `politica_x.pdf`. Changing `tokens` invalidates every existing sparse index — a full re-ingest is part of shipping any change to it.

### API conventions

- **Auth belongs in a `Depends`, never a middleware.** `api/auth.py` takes `Settings` as a parameter so tests can swap it via `app.dependency_overrides[get_settings]`; the middlewares in `ratelimit.py` call `get_settings()` directly and are painful to test as a result — don't copy that pattern.
- Two routers in `routes.py`: `system_router` (open, `/health` only) and `router` (requires a bearer token). New endpoints go on `router` unless a probe needs to reach them without credentials.
- Tests mint their own RSA keypair and monkeypatch `auth._get_signing_key`, so the suite never reaches the network.
- **A `sector.<folder>` app role grants that folder by convention** (`SECTOR_ROLE_PREFIX`); the map is only for names Entra cannot spell. A mapped role uses its mapped value *only* — letting the convention also fire would widen the grant to a second folder named after the role.
- **The sectors dependency has three states, and two of them look alike.** `None` means "do not filter" (auth off, no header); `[]` means "reads nothing" and short-circuits before Qdrant is touched. Never collapse them into a falsy check — `if not sectors` would turn a caller who reads nothing into a caller who reads everything.

## Tech Stack Decisions

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Vector DB | Qdrant | Hybrid search built-in, no separate BM25 infra |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` | Free, fast, offline |
| Reranking | cross-encoder `ms-marco-MiniLM-L-6-v2` | Improves retrieval precision |
| Framework | FastAPI | Async, typed |
| LLM | GPT-4o-mini (default) | Cost-effective; Claude as alternative |
| Config | pydantic-settings | Env-based configuration |
| Auth | Azure Entra ID via `pyjwt[crypto]` | Resource-server validation only (JWKS, RS256); app roles → sector compartments |
| Remote sources | `httpx` + `msal` / `google-auth` | Plain REST against Graph and Drive; avoids the msgraph-sdk and google-api-python-client stacks |
| Chunking | LangChain text splitters only | Thin usage, no framework lock-in |
| Conversation history | MySQL via `pymysql`, raw SQL | No ORM, matching how the project talks to Qdrant and OpenAI. Pure-Python driver so the slim image needs no C toolchain |

## Commit Workflow

- Conventional commits: `feat:`, `fix:`, `docs:`, `ci:`, `test:`
- One logical change per commit
- Read `SPEC.md` for the incremental 6-phase commit plan before committing
- Use `/commit` to commit following the plan
