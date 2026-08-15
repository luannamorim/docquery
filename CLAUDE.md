# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

docquery is a production-grade RAG (Retrieval-Augmented Generation) system for querying technical documentation. It returns answers with citations and confidence scores, evaluated with RAGAS metrics. API-only — no frontend, no streaming, no chat history. Authentication is Azure Entra ID bearer-token validation, opt-in via `AUTH_ENABLED`.

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

Source layout under `src/docquery/`: `config.py`, `ingest/` (loader, sources, chunker, pipeline), `retrieve/` (embedder, hybrid, reranker), `generate/` (rag), `api/` (app, routes, schemas, auth). Eval lives in `eval/`.

### Ingest conventions

- **A document's `source` is its identity.** Deduplication, orphan pruning and the clearance/type policies all match on it by prefix. Remote documents are indexed under their URI, never the temporary path they were downloaded to.
- Prefix matching must be bounded by a separator (`orphan_prefix_for`, `is_allowed_uri`) — an unterminated prefix silently captures sibling folders.
- `sources.py` dispatches by URI scheme through a dict of functions, mirroring `LOADERS` in `loader.py`. Adding a connector means adding a fetcher and a validator, not a class hierarchy.
- Remote fetch tests drive `httpx.MockTransport` rather than patching internals, so pagination, streaming and the size cap are actually exercised.

### API conventions

- **Auth belongs in a `Depends`, never a middleware.** `api/auth.py` takes `Settings` as a parameter so tests can swap it via `app.dependency_overrides[get_settings]`; the middlewares in `ratelimit.py` call `get_settings()` directly and are painful to test as a result — don't copy that pattern.
- Two routers in `routes.py`: `system_router` (open, `/health` only) and `router` (requires a bearer token). New endpoints go on `router` unless a probe needs to reach them without credentials.
- Tests mint their own RSA keypair and monkeypatch `auth._get_signing_key`, so the suite never reaches the network.

## Tech Stack Decisions

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Vector DB | Qdrant | Hybrid search built-in, no separate BM25 infra |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` | Free, fast, offline |
| Reranking | cross-encoder `ms-marco-MiniLM-L-6-v2` | Improves retrieval precision |
| Framework | FastAPI | Async, typed |
| LLM | GPT-4o-mini (default) | Cost-effective; Claude as alternative |
| Config | pydantic-settings | Env-based configuration |
| Auth | Azure Entra ID via `pyjwt[crypto]` | Resource-server validation only (JWKS, RS256); app roles → clearance levels |
| Remote sources | `httpx` + `msal` / `google-auth` | Plain REST against Graph and Drive; avoids the msgraph-sdk and google-api-python-client stacks |
| Chunking | LangChain text splitters only | Thin usage, no framework lock-in |

## Commit Workflow

- Conventional commits: `feat:`, `fix:`, `docs:`, `ci:`, `test:`
- One logical change per commit
- Read `SPEC.md` for the incremental 6-phase commit plan before committing
- Use `/commit` to commit following the plan
