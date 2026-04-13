from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI

from docquery.api.routes import router
from docquery.config import get_settings
from docquery.retrieve.embedder import _get_model
from docquery.retrieve.reranker import _get_reranker


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _get_model(settings.embedding_model)
    _get_reranker(settings.reranker_model)
    yield


app = FastAPI(title="docquery", version=version("docquery"), lifespan=lifespan)
app.include_router(router)
