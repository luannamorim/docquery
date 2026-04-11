# API Reference

docquery exposes a JSON REST API via FastAPI. Interactive docs are available at `http://localhost:8000/docs` when the server is running.

## Starting the Server

```bash
# Development (with auto-reload)
make serve

# Production
make serve-prod

# Via Docker
docker compose up
```

## Endpoints

### GET /health

Returns server status.

**Response:**
```json
{"status": "ok"}
```

**Example:**
```bash
curl http://localhost:8000/health
```

---

### POST /query

Query the knowledge base and receive a cited answer.

**Request body:**
```json
{
  "query": "string"
}
```

**Response:**
```json
{
  "answer": "string",
  "sources": [
    {
      "index": 1,
      "source": "string",
      "chunk_index": 0,
      "score": 0.0,
      "text": "string"
    }
  ],
  "query": "string",
  "model": "string"
}
```

**Example:**
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What embedding model is used?"}'
```

**Notes:**
- Requires Qdrant running with documents ingested
- Requires `OPENAI_API_KEY` set
- Response time depends on retrieval + reranking + LLM latency (~2-5s)

---

### POST /ingest

Ingest a file or directory into the knowledge base.

**Request body:**
```json
{
  "path": "string"
}
```

**Response:**
```json
{
  "chunks": 42,
  "path": "string"
}
```

**Error responses:**
- `404` — path does not exist on the server

**Example:**
```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"path": "docs/sample"}'
```

**Notes:**
- The path is resolved on the server, not uploaded by the client
- Supports `.md`, `.txt`, `.pdf` files
- Re-ingesting the same files adds duplicate chunks (deduplication is not implemented)
- Returns the number of chunks stored, not the number of files processed

## Error Handling

All errors return standard HTTP status codes with a JSON body:

```json
{
  "detail": "error message"
}
```

| Status | Meaning |
|---|---|
| `404` | Path not found (ingest endpoint) |
| `422` | Request validation error (missing or invalid fields) |
| `500` | Internal server error (pipeline failure) |

## OpenAPI Schema

The full OpenAPI schema is available at `http://localhost:8000/openapi.json`. Interactive Swagger UI is at `http://localhost:8000/docs`.
