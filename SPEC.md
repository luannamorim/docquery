# `docquery` — Production RAG System for Technical Documentation

## Briefing for Claude Code Agent

---

## 1. Critical Review of the Original Prompt

Before building, here's what a senior engineer would flag:

**What's good:**
- Problem is real and well-scoped
- Emphasis on evaluation (RAGAS) separates this from toy projects
- Incremental commits show engineering maturity

**What needs fixing:**

| Original | Problem | Fix |
|----------|---------|-----|
| "LangChain/LlamaIndex" | Pick one. Both = indecision, not flexibility | LlamaIndex (simpler, less abstraction leakage) |
| "ChromaDB or Qdrant" | Same problem. Choose. | Qdrant — production-grade, has hybrid search built-in |
| "OpenAI Embeddings" | Vendor lock-in, costs money, no offline dev | Start with `all-MiniLM-L6-v2` (free, fast). Add OpenAI as optional provider |
| No hybrid retrieval | Pure vector search is a known failure mode in 2026 | BM25 + dense vectors (hybrid) via Qdrant's built-in support |
| No reranking | Retrieval without reranking = noisy results | Add cross-encoder reranking step (lightweight, big impact) |
| PostgreSQL for metadata | Over-engineering for a portfolio project | Qdrant handles metadata filtering natively. Drop Postgres. |
| No chunking strategy defined | "Documented strategy" is vague | Define: semantic chunking with fallback to fixed-size + overlap |
| Missing: async ingestion | Single-threaded ingestion = demo, not production | Async ingestion pipeline (separate from query pipeline) |
| Missing: caching | Every query hits LLM = expensive and slow | Semantic cache layer for repeated/similar queries |

---

## 2. Refined Architecture

```
docs (markdown/pdf)
    │
    ▼
[Ingestion Pipeline]  ←── async, independent
    │
    ├── Document Loader (markdown, PDF, txt)
    ├── Chunker (semantic + fixed-size fallback)
    ├── Embedder (sentence-transformers, swappable)
    └── Store → Qdrant (vectors + sparse BM25 index)
    
[Query Pipeline]  ←── FastAPI serves this
    │
    ├── Query → Embed
    ├── Hybrid Retrieval (dense + BM25 via Qdrant)
    ├── Reranker (cross-encoder)
    ├── Context Assembly (with source metadata)
    ├── LLM Generation (with citations)
    └── Response + Sources + Confidence Score
    
[Evaluation]  ←── offline, runs against eval dataset
    │
    ├── RAGAS metrics (faithfulness, relevancy, context precision)
    ├── Eval dataset (question + expected_answer + source_doc)
    └── Before/after comparison on each improvement
```

---

## 3. Project Structure

```
docquery/
├── src/
│   └── docquery/
│       ├── __init__.py
│       ├── config.py            # pydantic-settings, env-based
│       ├── ingest/
│       │   ├── __init__.py
│       │   ├── loader.py        # document loaders (md, pdf, txt)
│       │   ├── chunker.py       # chunking strategies
│       │   └── pipeline.py      # orchestrates load→chunk→embed→store
│       ├── retrieve/
│       │   ├── __init__.py
│       │   ├── embedder.py      # embedding provider (swappable)
│       │   ├── hybrid.py        # hybrid retrieval (dense + sparse)
│       │   └── reranker.py      # cross-encoder reranking
│       ├── generate/
│       │   ├── __init__.py
│       │   └── rag.py           # context assembly + LLM call + citation
│       └── api/
│           ├── __init__.py
│           ├── app.py           # FastAPI app
│           ├── routes.py        # /query, /ingest, /health
│           └── schemas.py       # request/response models
├── eval/
│   ├── dataset.json             # eval questions + expected answers
│   ├── run_eval.py              # RAGAS evaluation runner
│   └── results/                 # stored eval results (before/after)
├── docs/
│   └── sample/                  # sample technical docs for demo
├── tests/
│   ├── test_chunker.py
│   ├── test_retrieval.py
│   └── test_api.py
├── docker-compose.yml           # app + qdrant
├── Dockerfile
├── pyproject.toml
├── Makefile                     # make ingest, make serve, make eval
└── README.md
```

---

## 4. Tech Stack (Final)

| Layer | Technology | Why |
|-------|-----------|-----|
| Framework | FastAPI | Async, typed, industry standard |
| Embeddings | sentence-transformers (`all-MiniLM-L6-v2`) | Free, fast, good baseline. OpenAI as optional swap |
| Vector DB | Qdrant | Hybrid search built-in, production-ready, no Postgres needed |
| Reranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Lightweight, big quality gains |
| LLM | OpenAI GPT-4o-mini (or Claude via API) | Cost-effective for generation |
| Chunking | LangChain text splitters (just the splitters, not the framework) | Proven, minimal dependency |
| Evaluation | RAGAS | Industry standard for RAG eval |
| Infra | Docker + Docker Compose | Reproducible, one-command setup |
| Config | pydantic-settings | Typed env config |

---

## 5. Commit Plan (Incremental)

Each commit = a working state. No broken commits.

### Phase 1 — Foundation
```
feat: initialize project structure with pyproject.toml and Makefile
feat: add pydantic-settings config with env support
feat: add Docker and docker-compose with Qdrant service
```

### Phase 2 — Ingestion Pipeline
```
feat: add document loaders for markdown, pdf, and txt
feat: add chunking strategies (semantic + fixed-size fallback)
feat: add embedding provider with sentence-transformers
feat: add ingestion pipeline orchestrator with Qdrant storage
```

### Phase 3 — Query Pipeline
```
feat: add hybrid retrieval (dense + BM25) via Qdrant
feat: add cross-encoder reranking step
feat: add RAG generation with citation extraction
```

### Phase 4 — API
```
feat: add FastAPI app with /health endpoint
feat: add /query endpoint with request/response schemas
feat: add /ingest endpoint for document upload
```

### Phase 5 — Evaluation
```
feat: add eval dataset with 20 question-answer pairs
feat: add RAGAS evaluation runner with metrics output
docs: add eval results baseline to results/
```

### Phase 6 — Polish
```
feat: add sample technical docs for demo
docs: add README with problem, architecture, decisions, and quickstart
docs: add architecture diagram (mermaid in README)
ci: add GitHub Actions for lint + tests
```

---

## 6. README Skeleton

The README should follow this structure:

1. **One-liner** — what it does in one sentence
2. **Problem** — why this exists (2-3 sentences)
3. **Architecture diagram** — mermaid diagram
4. **Quickstart** — `docker compose up` + `make ingest` + `make query`
5. **Technical decisions** — table with decision, options considered, why this choice
6. **Evaluation results** — RAGAS metrics table (before/after each improvement)
7. **API reference** — endpoints with example curl commands
8. **Project structure** — tree view

---

## 7. Key Engineering Decisions to Document

These go in the README and show senior-level thinking:

| Decision | Options Considered | Choice | Rationale |
|----------|--------------------|--------|-----------|
| Vector DB | ChromaDB, Qdrant, Pinecone | Qdrant | Built-in hybrid search, no separate BM25 infra needed |
| Embeddings | OpenAI, Cohere, sentence-transformers | sentence-transformers (default) | Zero cost, no API dependency, swappable via config |
| Chunking | Fixed-size, semantic, page-based | Semantic with fixed-size fallback | Best balance of coherence and reliability |
| Reranking | None, LLM-based, cross-encoder | Cross-encoder | 50ms latency, measurable quality improvement, no LLM cost |
| Framework | LangChain, LlamaIndex, custom | Thin custom + individual libs | No framework lock-in, explicit control over pipeline |
| Eval | Manual testing, RAGAS, custom | RAGAS | Industry standard, reproducible, comparable metrics |
| Content taxonomy | Collection-per-folder, configured type policy, derived metadata facets | Facets derived from the ingested tree, single collection | Qdrant `Filter` scopes retrieval without fragmenting the index; deriving facets from the folders the corpus already has leaves nothing to configure or keep in sync |

### Content taxonomy & metadata model

The corpus is heterogeneous (e.g. contracts, policies, manuals), not a single
technical-doc set. Organization is done with **metadata-filtered hybrid
retrieval in one collection**, not separate collections or a rigid hierarchy.

Each chunk carries:

- `folders` — the folder segments of the document's path relative to the root
  that was ingested, derived **server-side** at ingest time. The corpus
  structure is the taxonomy, so no configuration restates it. Because `folders`
  gates retrieval scope, it is **not** read from frontmatter (untrusted authors
  must not self-label), exactly like `sector`.
  (Superseded a `doc_type` facet configured by path prefix in `type_policy`:
  for a SharePoint library it duplicated by hand the folder names already
  present in every source URI.)
- `entity`, `tags`, `title` — descriptive facets that may come from frontmatter
  (non-security; allowlisted in the loader).
- `sector` — the access compartment: the document's top-level folder, derived
  server-side. Deliberately not the `folders` list, which matches at any
  depth and would hand a nested folder's name the wrong compartment.
- `source`, `section` — as before.

Queries default to **global** retrieval and accept optional scoping filters
(`folders`, `source`, `tags`) that are ANDed with the always-on sector
filter. A folder name matches at any depth. `folders` is surfaced in each
citation for transparency.

---

## 8. What NOT to Build

Keep scope tight. This is a portfolio piece, not a startup:

- ~~Frontend/UI — API only, curl examples are enough~~ — **built anyway** (see below)
- ~~Auth/RBAC — mention it as "production consideration" in README~~ — **built anyway** (see below)
- Multiple LLM providers — one provider, mention swappability in config
- ~~Streaming responses — nice-to-have, not core value~~ — **built anyway** (see below)
- ~~Chat history/memory — this is a Q&A system, not a chatbot~~ — **built anyway** (see below)
- ~~Complex document parsing (OCR, tables) — markdown + PDF text is enough~~ — **built anyway** (Docling)

### Delivered beyond this scope

Two items above were deliberately reopened after the original six phases, because the "production-grade" claim did not survive them being missing:

- **Document parsing** — Docling ingestion (OCR, table structure, DOCX/PPTX/XLSX).
- **Auth/RBAC** — Azure Entra ID bearer-token validation (`AUTH_ENABLED`) with app roles mapped to sector compartments: each document carries the top-level folder it lives in, each token the sectors its roles grant, and retrieval returns the intersection. Shipped first as a numeric clearance ladder, replaced once it became clear that levels nest and sectors do not — RH and Financeiro exclude each other, which no ordering can express.
- **Remote sources** — ingestion from SharePoint and Google Drive folders by URI, pulled one-shot into a temporary directory. Not in the original scope either way; added because a corpus that only exists on a local disk is not the case the system is for.
- **Conversation history** — `conversation_id`, follow-up resolution and an audit trail in MySQL (`HISTORY_ENABLED`). Reopened because a stateless `/query` breaks on the second question a person asks: "e a multa por atraso?" reaches retrieval with no anchor and matches whatever shares the word. A first turn is still never rewritten, so the RAGAS baseline stays comparable.
- **Frontend and streaming** — a small TypeScript SPA served by the API itself, fed by an SSE endpoint that emits citations before the first token. These two fell together: an internal system whose users are HR and finance staff has no users if the only client is curl, and a chat interface that stares at a spinner for several seconds is not one people will keep using. `POST /query` remains unstreamed and unchanged — it is what `run_eval.py` measures.

The pattern across all of these is the same one the auth section names: each was excluded as *portfolio scope*, and each became necessary once the system acquired real users and a real corpus. What has **not** been reopened is the framework question — there is still no LangChain/LlamaIndex orchestration, no ORM, and no frontend framework.

---

## 9. Success Criteria

The project is "done" when:

- [ ] `docker compose up` → everything runs
- [ ] `make ingest docs/sample/` → documents indexed
- [ ] `curl /query` → returns answer with citations and sources
- [ ] `make eval` → prints RAGAS metrics table
- [ ] README explains every architectural decision
- [ ] Eval results show before/after improvement from at least one optimization (e.g., adding reranking)

---

## 10. Agent Instructions

When starting with Claude Code:

1. **Read this entire briefing first**
2. **Follow the commit plan exactly** — one feature per commit
3. **Use conventional commits** — `feat:`, `fix:`, `docs:`, `ci:`, `test:`
4. **Minimal code** — no over-abstraction, no unnecessary classes, functions > classes
5. **Type hints everywhere** — this is a Python project in 2026
6. **Tests only for core logic** — chunker, retrieval, API routes. Not 100% coverage theater.
7. **When in doubt, keep it simple** — a working 200-line file beats a 50-file architecture
