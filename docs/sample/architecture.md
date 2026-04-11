# docquery Architecture

docquery is a production-grade RAG (Retrieval-Augmented Generation) system for querying technical documentation. It returns answers with inline citations and confidence scores, evaluated with RAGAS metrics.

## System Overview

The system is built around three independent pipelines:

- **Ingestion** — loads documents, chunks them, embeds them, and stores vectors in Qdrant
- **Query** — embeds a query, retrieves relevant chunks via hybrid search, reranks them, and generates an answer
- **Evaluation** — runs RAGAS metrics against a test dataset to measure retrieval and generation quality

## Technology Choices

### Vector Database: Qdrant

Qdrant was chosen over ChromaDB and Pinecone because it has hybrid search (dense + sparse vectors) built in. This eliminates the need for a separate BM25 indexing service. Qdrant's server-side Reciprocal Rank Fusion (RRF) merges dense and sparse results without any client-side code.

### Embeddings: sentence-transformers all-MiniLM-L6-v2

The `all-MiniLM-L6-v2` model produces 384-dimensional dense vectors. It runs entirely offline, requires no API key, and is fast enough for real-time query embedding. It can be swapped for an OpenAI embedding model via the `EMBEDDING_MODEL` environment variable.

### Reranking: cross-encoder ms-marco-MiniLM-L-6-v2

After initial hybrid retrieval returns up to 20 candidates, a cross-encoder reranker rescores each query-document pair. Cross-encoders are slower than bi-encoders (they process query and document together rather than independently) but significantly more accurate. This two-stage approach balances recall at the retrieval step with precision at the reranking step.

### LLM: OpenAI GPT-4o-mini

GPT-4o-mini is used for answer generation. It is cost-effective, fast, and capable of following citation instructions reliably. The model name is configurable via the `LLM_MODEL` environment variable.

### Framework: FastAPI

FastAPI provides async-capable HTTP endpoints with automatic OpenAPI documentation. The underlying pipeline code uses synchronous libraries (sentence-transformers, qdrant-client, openai), so endpoint handlers use sync `def` functions, which FastAPI runs in a thread pool.

## Data Flow

```
User Query
    │
    ▼
Embed Query (all-MiniLM-L6-v2)
    │
    ├── Dense vector (384-dim)
    └── Sparse vector (BM25 term frequencies)
    │
    ▼
Hybrid Retrieval (Qdrant)
    │
    ├── Dense search (cosine similarity)
    ├── Sparse search (BM25 + IDF)
    └── RRF fusion (server-side)
    │
    ▼
Reranking (cross-encoder)
    │
    ▼
Context Assembly (top-k chunks with metadata)
    │
    ▼
LLM Generation (GPT-4o-mini with citations)
    │
    ▼
Response: answer + sources + model
```
