<div align="center">

<img src="docs/public/banner.svg" alt="docquery" width="380"/>

**Production-ready RAG system for technical documentation.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Qdrant](https://img.shields.io/badge/Qdrant-vector--db-DC244C)](https://qdrant.tech/)
[![RAGAS Faithfulness](https://img.shields.io/badge/RAGAS%20faithfulness-0.893%20±%200.010-7C3AED)](eval/results/baseline.json)
[![RAGAS Recall](https://img.shields.io/badge/RAGAS%20context%20recall-0.749%20±%200.024-7C3AED)](eval/results/baseline.json)

docquery combines **hybrid search (dense + BM25)**, **cross-encoder reranking**, and **citation-grounded generation** for accurate, verifiable answers from your documentation corpus. Evaluated end-to-end with RAGAS metrics.

</div>

---

## I built this. Then I audited it.

The v1 of docquery had strong foundations: hybrid retrieval, reranking, RAGAS evaluation, idempotent ingest. But "working" and "defensible" are different standards. This sprint closed six measurable gaps:

| Gap | Before | After |
|-----|--------|-------|
| Cost tracking | No visibility into tokens/cost per query | `tokens_in`, `tokens_out`, `cost_usd` in every API response and eval run |
| Gold-set size | 20 questions (low statistical power) | 101 stratified questions: factual, multi-hop, comparative, unanswerable |
| Chunking strategy | Hardcoded Markdown + Recursive | Configurable via `CHUNKER_STRATEGY=markdown\|recursive\|semantic` |
| Prompt injection | No input validation — any payload reached the LLM | Input guard blocks OWASP LLM01/LLM06 patterns at the API boundary |
| RBAC | All documents accessible to all users | `clearance_level` per chunk; `X-User-Clearance` header filters retrieval |
| Self-audit narrative | None | This README |

The tradeoff for hardening instead of starting a new project: six gaps closed in ~1.5 weeks, narrative of "engineer auditing their own work" — which is rarer and more credible in a portfolio than project #N.

---

## Problem

Technical teams accumulate large volumes of documentation — architecture docs, runbooks, API references — that are expensive to search manually. Generic keyword search misses semantic intent; LLMs hallucinate without grounding. docquery combines hybrid retrieval (dense + BM25) with cross-encoder reranking and citation-grounded generation to produce accurate, verifiable answers from your own documentation corpus.

## Architecture

```mermaid
flowchart TD
    subgraph Ingestion
        A[Documents\nmd / pdf / txt] --> B[Loader\ningest_root allowlist]
        B --> C[Chunker\nmarkdown · recursive · semantic]
        C --> Y[Clearance Policy\npath_prefix → level]
        Y --> D[Embedder\nall-MiniLM-L6-v2]
        D --> E[(Qdrant\ndense + sparse\nclearance_level index)]
    end

    subgraph Query
        F[User Query] --> G[Guard\ninjection check]
        G --> H[Embed Query]
        H --> I[Hybrid Retrieval\nRRF + clearance filter]
        I --> J[Cross-Encoder\nReranker]
        J --> K[LLM Generation\nGPT-4o-mini]
        K --> L[Answer + Citations\n+ tokens + cost]
    end

    subgraph Evaluation
        M[eval/dataset_v2.json\n101 stratified questions] --> N[query_pipeline]
        N --> O[RAGAS Metrics\nfaithfulness · relevancy\nprecision · recall · cost]
    end

    E --> I
    X[X-User-Clearance header] --> I
```

## Quickstart

**Prerequisites:** Docker, an OpenAI API key.

```bash
# 1. Start app + Qdrant
cp .env.example .env
# Add your OPENAI_API_KEY to .env
docker compose up

# 2. Ingest sample docs (via API)
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"path": "docs/sample"}'

# 3. Query
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "How does hybrid search work?"}'

# 4. Evaluate (runs locally against the running API)
uv sync --extra eval
make eval
```

**Local dev (no Docker):**

```bash
docker run -p 6333:6333 qdrant/qdrant
uv sync --extra dev
make serve
```

## Technical Decisions

| Decision       | Options Considered                    | Choice                                                          | Rationale |
| -------------- | ------------------------------------- | --------------------------------------------------------------- | --------- |
| Vector DB      | ChromaDB, Qdrant, Pinecone            | **Qdrant**                                                      | Built-in hybrid search + RRF fusion, payload indexing for RBAC filters |
| Embeddings     | OpenAI, Cohere, sentence-transformers | **all-MiniLM-L6-v2**                                            | Zero cost, offline, swappable via config |
| Sparse vectors | fastembed/BM25, SPLADE, manual TF     | **Manual TF + Modifier.IDF**                                    | No extra deps; Qdrant handles IDF at query time |
| Chunking       | Fixed-size, semantic, page-based      | **MarkdownHeaderTextSplitter (default) + configurable**         | Splits by H1/H2/H3 so every chunk carries a breadcrumb section; `CHUNKER_STRATEGY=semantic\|recursive` available for comparison |
| Reranking      | None, LLM-based, cross-encoder        | **cross-encoder/ms-marco-MiniLM-L-6-v2**                        | ~50ms latency, measurable quality gain, no LLM cost |
| Framework      | LangChain, LlamaIndex, custom         | **Thin custom + individual libs**                               | No framework lock-in, explicit pipeline control |
| Evaluation     | Manual, RAGAS, custom                 | **RAGAS 0.4.x**                                                 | Industry standard, reproducible, comparable metrics |
| Config         | dotenv, Dynaconf, pydantic-settings   | **pydantic-settings**                                           | Type-safe, env-based, integrates with FastAPI DI |
| RBAC           | JWT decode, header, body field        | **Server-side `clearance_policy` + `X-User-Clearance` header**  | Honest: no auth service in scope. Classification is server-side (frontmatter ignored), bound-checked header, audit-logged on use |
| Injection guard | Llama Guard, NeMo Guardrails, custom | **NFKC-normalized regex validator (guard.py)**                  | Zero latency, zero dependencies, covers OWASP LLM01/LLM06 patterns in EN + PT-BR/ES, NFKC handles fullwidth-Latin evasions; second layer is hardened system prompt; third is `check_context()` over retrieved chunks |

## Evaluation Results

### RAGAS Baseline

Measured on `docs/sample/` (7 documents, ~65 chunks after hardening corpus), GPT-4o-mini generator. Aggregate of 3 sequential runs (mean ± stdev) to account for LLM-judge variance.

| Metric            | Description                            | Baseline (v1, 20q) | With dataset_v2 (101q) |
| ----------------- | -------------------------------------- | ------------------ | ---------------------- |
| Faithfulness      | Answer grounded in retrieved context   | **0.893 ± 0.010**  | run `make eval-v2`     |
| Answer Relevancy  | Answer addresses the question          | **0.909 ± 0.002**  | run `make eval-v2`     |
| Context Precision | Retrieved contexts ranked by relevance | **0.931 ± 0.002**  | run `make eval-v2`     |
| Context Recall    | All relevant information retrieved     | **0.749 ± 0.024**  | run `make eval-v2`     |

Full baseline in [`eval/results/baseline.json`](eval/results/baseline.json). Historical snapshots preserved in `eval/results/milestones/`.

To reproduce: `uv sync --extra eval && make eval`. Ad-hoc runs are written to `eval/results/<timestamp>.json` and gitignored.

### Reranker Ablation

Run `python eval/scripts/ablation_reranker.py` to compare RAGAS scores and cost/query with and without the cross-encoder. Results are saved to `eval/results/ablation/`. Expected: precision and recall improve with reranker; cost may decrease as context sent to LLM is smaller.

### Chunking Strategy Comparison

Run `make compare-chunkers` to evaluate `markdown`, `recursive`, and `semantic` strategies on `dataset_v2.json`. Results in `eval/results/chunker_comparison/`. Default (`markdown`) is expected to outperform `recursive` for structured technical docs; `semantic` trades ingestion latency for potentially better multi-hop recall.

> **On methodology.** In a production setting this would live in an experiment tracker (MLflow, Weights & Biases) with CI-gated eval and regression thresholds. The committed JSON snapshots document methodology and results without extra infrastructure.

## RBAC — Clearance-Level Access Control

Chunks carry an integer `clearance_level` payload field. **Classification is server-side**: the level is assigned at ingest time from `settings.clearance_policy` — a list of `(path_prefix, level)` tuples, first match wins — never from the document itself. Frontmatter `clearance:` is parsed but explicitly **ignored** with a log warning, because an untrusted ingest author could otherwise self-label sensitive content as public.

Configure the policy via env (`pydantic-settings` parses JSON):

```bash
CLEARANCE_POLICY='[["docs/sample/internal_architecture.md", 5], ["docs/sample/", 0]]'
DEFAULT_CLEARANCE_LEVEL=0   # set above MAX_CLEARANCE_LEVEL for fail-closed prod
MAX_CLEARANCE_LEVEL=10      # ceiling enforced on X-User-Clearance header
```

At query time, pass `X-User-Clearance`. Only chunks with `clearance_level ≤ X-User-Clearance` are retrieved. The filter is applied at **both** the hybrid retrieval step (`hybrid.py`) and the context expansion step (`expand.py`) — the second is the easy-to-miss leak point where a privileged neighbor could otherwise be appended to a public hit's window.

**Demo — same query, different clearance:**

```bash
# Public user (clearance 0) — cannot see internal architecture content
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-User-Clearance: 0" \
  -d '{"query": "What are the internal cost targets?"}'
# → "I couldn't find relevant information to answer that question."

# Privileged user (clearance 5) — sees internal_architecture.md content
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-User-Clearance: 5" \
  -d '{"query": "What are the internal cost targets?"}'
# → "The engineering team targets a mean cost of under $0.002 per query [1]..."
```

> In production, `X-User-Clearance` would be derived from a verified JWT claim, not a raw header. The header is bound-checked against `MAX_CLEARANCE_LEVEL` and logged on use; the filter logic is production-ready, the auth transport is not.

## Prompt Injection Guard

The `/query` endpoint validates input before it reaches the retrieval pipeline. `src/docquery/api/guard.py` NFKC-normalizes the query (flattening fullwidth-Latin homoglyphs) then runs regex/heuristic checks:

| Layer | What it catches |
|-------|----------------|
| Instruction override | `ignore previous instructions`, `bypass all constraints`, PT-BR/ES equivalents (`ignore as instruções`, `esqueça as regras`, `desconsidere`) |
| Role injection | `system: ...`, `<\|im_start\|>`, `### System`, `<sys>` tags, PT-BR `sistema:` |
| Prompt leak | Verb + qualifier + prompt-noun pattern (`reveal your system prompt`, `repeat your initial instructions`); bare `instructions` no longer triggers false positives like "What are the instructions to configure X?" |
| Jailbreak | DAN, `act as an unrestricted AI`, persona switches, PT-BR `finja que é` / `aja como` |
| Structural | Inputs above `GUARD_MAX_QUERY_LENGTH` (default 2000), disallowed Unicode `Cf` chars (RLO, ZWSP, ...) — ZWJ/ZWNJ/LRM/RLM/BOM allow-listed so emoji ZWJ sequences and bidi marks pass |
| Indirect injection | `check_context()` re-applies override + role-injection regexes to **retrieved chunks** and logs a WARN when a poisoned doc is fetched — defence in depth, not a hard block (indexed docs may legitimately contain attack examples) |

Blocked requests return `HTTP 400` with a reason string. The second layer is the hardened `SYSTEM_PROMPT` in `rag.py`, which explicitly instructs the LLM not to reveal instructions or adopt different roles.

**Run the full injection suite** (regex-only, no API key needed):

```bash
python eval/security/injection_suite.py
# → eval/results/security/injection_v1.json
```

The suite covers **47 attacks** across OWASP LLM Top 10 categories — 36 expected-block (direct injection, role injection, prompt leak, jailbreak, structural, PT-BR + NFKC evasions) and 11 benign/borderline — and targets **≥ 95% block rate** (currently 100%).

## API Reference

### `GET /health`

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### `POST /query`

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-User-Clearance: 0" \
  -d '{"query": "What chunking strategy is used?"}'
```

```json
{
  "answer": "Markdown files are split using MarkdownHeaderTextSplitter [1]...",
  "sources": [{"index": 1, "source": "docs/sample/ingestion.md", "chunk_index": 2, "score": 9.4, "text": "...", "section": "Ingestion Pipeline > Chunking"}],
  "query": "What chunking strategy is used?",
  "model": "gpt-4o-mini",
  "tokens_in": 842,
  "tokens_out": 87,
  "cost_usd": 0.000178
}
```

### `POST /ingest`

Returns `202 Accepted`. Ingestion runs in the background.

```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"path": "docs/sample"}'
# {"task_id": "e3b0c442-...", "status": "pending"}
```

### `GET /ingest/{task_id}`

```bash
curl http://localhost:8000/ingest/e3b0c442-...
# {"task_id": "e3b0c442-...", "status": "done", "chunks": 65, "deleted": 0, "error": null}
```

Interactive docs: `http://localhost:8000/docs`

## Project Structure

```
docquery/
├── src/docquery/
│   ├── config.py              # pydantic-settings env config
│   ├── ingest/
│   │   ├── loader.py          # document loaders (md, pdf, txt) + frontmatter RBAC
│   │   ├── chunker.py         # markdown / recursive / semantic strategies
│   │   ├── sparse.py          # BM25 sparse vector computation
│   │   └── pipeline.py        # ingestion orchestrator + clearance_level payload
│   ├── retrieve/
│   │   ├── embedder.py        # sentence-transformers wrapper
│   │   ├── hybrid.py          # hybrid retrieval with RRF + clearance filter
│   │   ├── reranker.py        # cross-encoder reranking
│   │   └── expand.py          # context expansion with clearance guard
│   ├── generate/
│   │   └── rag.py             # context assembly + LLM + citations + cost tracking
│   └── api/
│       ├── app.py             # FastAPI app
│       ├── guard.py           # prompt injection input validator
│       ├── routes.py          # /health, /query (guard + RBAC), /ingest
│       └── schemas.py         # request/response models (+ tokens_in/out/cost_usd)
├── eval/
│   ├── dataset.json           # v1: 20 question-answer pairs
│   ├── dataset_v2.json        # v2: 101 stratified questions (factual/multi-hop/comparative/unanswerable)
│   ├── run_eval.py            # RAGAS evaluation runner + cost tracking
│   ├── scripts/
│   │   ├── generate_v2.py     # LLM-as-generator for dataset expansion
│   │   ├── compare_chunkers.py # eval across markdown/recursive/semantic
│   │   └── ablation_reranker.py # reranker on vs off
│   ├── security/
│   │   └── injection_suite.py # 47-attack OWASP LLM Top 10 test suite (incl. PT-BR + NFKC evasions)
│   └── results/               # timestamped JSON results (baseline.json committed)
├── docs/sample/               # sample docs for demo (incl. internal_architecture.md clearance:5)
├── tests/                     # pytest: chunker, API, RBAC, guard, cost
├── .github/workflows/
│   ├── ci.yml                 # lint + pytest (no API key needed)
│   └── security-suite.yml     # injection suite (workflow_dispatch, OPENAI_API_KEY)
├── docker-compose.yml
├── Dockerfile
├── Makefile
└── pyproject.toml
```

## Collection Management

| Action             | Command                                              |
| ------------------ | ---------------------------------------------------- |
| Open dashboard     | `http://localhost:6333/dashboard`                    |
| Inspect collection | `GET http://localhost:6333/collections/documents`    |
| Reset index        | `DELETE http://localhost:6333/collections/documents` |

Directory ingest is fully idempotent: chunk IDs are the first 128 bits of `SHA256(source \0 chunk_index \0 text)`, so re-ingesting the same file updates in place. Including `chunk_index` in the key prevents silent overwrites when a document has repeated text (boilerplate, repeated table rows, recurring section headers). Deleted files have their chunks cleaned up automatically on the next ingest.

## Production Considerations

Hardened in a follow-up security/code-review pass (full per-commit detail in `git log`):

- Path-prefix allowlist on `/ingest` against `INGEST_ROOT`, with symlink filtering.
- Server-side clearance via `CLEARANCE_POLICY` (frontmatter ignored); `MAX_CLEARANCE_LEVEL` ceiling on the header.
- In-memory rate limit (`RATE_LIMIT_REQUESTS_PER_MINUTE`), `Content-Length` cap (`REQUEST_MAX_BODY_BYTES`), and security headers (`X-Content-Type-Options`, `Referrer-Policy`, `Cache-Control: no-store`).
- OpenAI client `timeout` + `max_retries` from settings.
- Qdrant kept on the internal docker network with `QDRANT_API_KEY` plumbed through.
- Ingest task store with TTL + max size eviction.
- Generic error responses; full tracebacks logged server-side only.

Not implemented (still out of scope for a portfolio project):

- **Auth** — `X-User-Clearance` is an unauthenticated header. In prod, derive from a verified JWT claim.
- **Multi-worker rate limit / task store** — both are in-process. A real deployment with `uvicorn --workers N > 1` needs Redis (or Qdrant payload) for shared state.
- **Streaming** — responses could be streamed; OpenAI SDK supports it.
- **Chat history** — single-turn Q&A only, no conversation state.
- **Experiment tracking** — RAGAS results are committed JSON. In prod: MLflow or W&B with CI-gated eval.

## License

[MIT](https://github.com/luannamorim/docquery/blob/main/LICENSE)
