# Ingestion Pipeline

The ingestion pipeline processes documents from disk into Qdrant vector storage. It is run independently of the query pipeline and can be re-run to update the index.

## Running Ingestion

```bash
# Ingest a directory of documents
make ingest ARGS=docs/sample/

# Or run directly
python -m docquery.ingest.pipeline docs/sample/

# Via API
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"path": "docs/sample"}'
```

## Pipeline Stages

### 1. Document Loading

The loader supports three file formats:

- **Markdown (`.md`)** — read as plain text, preserving all markdown syntax
- **Plain text (`.txt`)** — read with UTF-8 encoding
- **PDF (`.pdf`)** — text extracted from all pages using pypdf

Unsupported formats are silently skipped when ingesting a directory.

### 2. Chunking

Documents are split into overlapping chunks using a file-type-aware strategy:

- **Markdown files** use `MarkdownTextSplitter`, which splits on headers (`#`, `##`, `###`) and code fences before falling back to paragraph and character boundaries. This preserves semantic structure.
- **All other files** use `RecursiveCharacterTextSplitter`, which tries paragraph, newline, then character boundaries.

Both strategies use the same `CHUNK_SIZE` (default: 512 characters) and `CHUNK_OVERLAP` (default: 50 characters) configuration values.

Chunk overlap ensures content near boundaries appears in adjacent chunks, improving recall for queries that span chunk edges.

### 3. Embedding

Each chunk is embedded into a 384-dimensional dense vector using `sentence-transformers all-MiniLM-L6-v2`. The model is loaded once and cached for the duration of the ingestion run.

### 4. Sparse Vector Computation

Each chunk also gets a BM25-style sparse vector for keyword search. Tokens are extracted by lowercasing and splitting on non-alphanumeric characters. Term frequencies are counted, then mapped to integer indices using a stable MD5 hash (to ensure consistency between ingestion and query time).

Qdrant applies IDF weighting at query time via the `Modifier.IDF` setting on the sparse vector field, so only raw term frequencies need to be stored.

### 5. Qdrant Storage

Chunks are stored as `PointStruct` objects with:

- **`vector["dense"]`** — 384-dim cosine-distance vector
- **`vector["sparse"]`** — BM25 term frequency sparse vector
- **`payload`** — `text`, `source` (file path), `chunk_index`, `file_type`

Points are upserted in batches of 100. The Qdrant collection is created automatically if it does not exist.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `QDRANT_HOST` | `localhost` | Qdrant server host |
| `QDRANT_PORT` | `6333` | Qdrant REST port |
| `QDRANT_COLLECTION` | `documents` | Collection name |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |
| `EMBEDDING_DIMENSION` | `384` | Vector dimension |
| `CHUNK_SIZE` | `512` | Max characters per chunk |
| `CHUNK_OVERLAP` | `50` | Overlap between adjacent chunks |
