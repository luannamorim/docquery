# Configuration

docquery separates two kinds of configuration: **runtime settings** (model names, connection details, chunk sizes) live in a `Settings` class backed by pydantic-settings; **application wiring** (FastAPI entry point) lives in `pyproject.toml`.

## Runtime Settings

Configuration uses pydantic-settings with a `Settings` class that reads from a `.env` file. All settings have sensible defaults and can be overridden via environment variables. This includes Qdrant connection details, model names, chunk sizes, and the OpenAI API key.

The class lives in `src/docquery/config.py`:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection: str = "documents"

    # Models
    embedding_model: str = "all-MiniLM-L6-v2"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    llm_model: str = "gpt-4o-mini"

    # Retrieval
    retrieval_top_k: int = 20
    reranker_top_k: int = 8

    # Chunking
    chunk_size: int = 1024
    chunk_overlap: int = 100

    # LLM
    openai_api_key: SecretStr = SecretStr("")
```

### Loading Order

Settings are resolved in this order (highest priority first):

1. Environment variables (e.g. `QDRANT_HOST=qdrant`)
2. Values from the `.env` file at the repo root
3. Defaults defined in the `Settings` class

Field names are case-insensitive: `QDRANT_HOST` and `qdrant_host` map to the same field.

### Dependency Injection

`get_settings()` is an `lru_cache`-decorated factory, so the same `Settings` instance is reused across the process. FastAPI routes depend on it via `Depends(get_settings)`, which makes tests trivial — override the dependency, swap the settings.

### Example `.env`

```env
OPENAI_API_KEY=sk-...
QDRANT_HOST=qdrant
LLM_MODEL=gpt-4o-mini
CHUNK_SIZE=1024
RETRIEVAL_TOP_K=20
```

## FastAPI Application Configuration

The application entry point is declared in `pyproject.toml` under `[tool.fastapi]`, pointing to `docquery.api.app:app`. The FastAPI CLI reads this configuration to locate the app for both development (with auto-reload) and production serving.

```toml
[tool.fastapi]
entrypoint = "docquery.api.app:app"
```

This lets the Makefile stay terse:

```makefile
serve:
	uv run fastapi dev

serve-prod:
	uv run fastapi run
```

Both `fastapi dev` and `fastapi run` discover the app via that `pyproject.toml` entry — no need to pass the module path on the command line.

### Runtime Container

The Docker image does not ship `uv` or `pyproject.toml` in the runtime stage. Instead, the container `CMD` invokes `uvicorn` directly:

```
python -m uvicorn docquery.api.app:app --host 0.0.0.0 --port 8000
```

The `[tool.fastapi]` entry is only consulted in local development, where the FastAPI CLI is available.
