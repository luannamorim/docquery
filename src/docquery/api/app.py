from fastapi import FastAPI

from docquery.api.routes import router

app = FastAPI(title="docquery", version="0.1.0")
app.include_router(router)
