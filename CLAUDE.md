# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

docquery is a production-grade RAG (Retrieval-Augmented Generation) system for querying technical documentation. It returns answers with citations and confidence scores, evaluated with RAGAS metrics. API-only — no frontend, no auth, no streaming, no chat history.

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

- **Ingestion** — Document Loader → Chunker (semantic + fixed-size fallback) → Embedder → Qdrant storage
- **Query** — Query Embedding → Hybrid Retrieval (dense + BM25 via Qdrant) → Cross-encoder Reranking → Context Assembly → LLM Generation → Response with citations
- **Evaluation** — RAGAS metrics (faithfulness, relevancy, context precision) with before/after comparison

Source layout under `src/docquery/`: `config.py`, `ingest/` (loader, chunker, pipeline), `retrieve/` (embedder, hybrid, reranker), `generate/` (rag), `api/` (app, routes, schemas). Eval lives in `eval/`.

## Tech Stack Decisions

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Vector DB | Qdrant | Hybrid search built-in, no separate BM25 infra |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` | Free, fast, offline |
| Reranking | cross-encoder `ms-marco-MiniLM-L-6-v2` | Improves retrieval precision |
| Framework | FastAPI | Async, typed |
| LLM | GPT-4o-mini (default) | Cost-effective; Claude as alternative |
| Config | pydantic-settings | Env-based configuration |
| Chunking | LangChain text splitters only | Thin usage, no framework lock-in |

## Commit Workflow

- Conventional commits: `feat:`, `fix:`, `docs:`, `ci:`, `test:`
- One logical change per commit
- Read `docs/BRIEFING.md` for the incremental 6-phase commit plan before committing
- Use `/commit` to commit following the plan
