# Query Pipeline

The query pipeline takes a natural language question, retrieves relevant document chunks, reranks them, and generates a cited answer using an LLM.

## Running a Query

```bash
# Via API
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "How does hybrid search work?"}'

# Programmatic
from docquery.generate.rag import query_pipeline
result = query_pipeline("How does hybrid search work?")
print(result["answer"])
```

## Pipeline Stages

### 1. Query Embedding

The query is embedded into two representations:

- **Dense vector** — encoded with the same `all-MiniLM-L6-v2` model used at ingestion time
- **Sparse vector** — tokenized with the same MD5-hashed term frequency approach used at ingestion

Using the same models and hashing scheme at both ingestion and query time ensures the vector spaces align correctly.

### 2. Hybrid Retrieval

Qdrant's `query_points` API is called with a `prefetch` list:

1. Dense search: cosine similarity over the `"dense"` vector field, returning `retrieval_top_k` candidates
2. Sparse search: BM25 with IDF over the `"sparse"` vector field, returning `retrieval_top_k` candidates

The top-level query uses `FusionQuery(fusion=Fusion.RRF)` to merge the two result sets server-side using Reciprocal Rank Fusion. RRF assigns each document a score based on its rank in each list: `score = Σ 1 / (k + rank)` where `k=60` by default.

This approach benefits from both recall types: dense search handles semantic queries ("how do embeddings work?") while sparse search handles keyword queries ("what is the Modifier.IDF parameter?").

### 3. Cross-Encoder Reranking

The top candidates from hybrid retrieval are reranked using `cross-encoder/ms-marco-MiniLM-L-6-v2`. Unlike the bi-encoder used for retrieval (which encodes query and document independently), the cross-encoder processes query and document together, enabling full attention between them. This produces more accurate relevance scores at the cost of higher latency.

Only the top `reranker_top_k` results (default: 5) are passed to the generation step.

### 4. Answer Generation

The top-k reranked chunks are assembled into a numbered context block:

```
[1] (source: docs/sample/architecture.md)
<chunk text>

[2] (source: docs/sample/query_pipeline.md)
<chunk text>
```

This context, along with the original query, is sent to GPT-4o-mini with a system prompt instructing it to answer based only on the provided context and cite passages inline using `[1]`, `[2]`, etc.

### 5. Response Format

The pipeline returns:

```json
{
  "answer": "Hybrid search combines... [1] ... The RRF algorithm... [2]",
  "sources": [
    {"index": 1, "source": "docs/sample/architecture.md", "chunk_index": 3, "score": 9.2, "text": "..."},
    {"index": 2, "source": "docs/sample/query_pipeline.md", "chunk_index": 1, "score": 8.7, "text": "..."}
  ],
  "query": "How does hybrid search work?",
  "model": "gpt-4o-mini"
}
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `RETRIEVAL_TOP_K` | `20` | Candidates per retrieval method |
| `RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Reranker model |
| `RERANKER_TOP_K` | `5` | Chunks passed to LLM |
| `OPENAI_API_KEY` | — | Required for generation |
| `LLM_MODEL` | `gpt-4o-mini` | OpenAI model |
| `LLM_TEMPERATURE` | `0.0` | Generation temperature |
| `LLM_MAX_TOKENS` | `1024` | Max response tokens |
