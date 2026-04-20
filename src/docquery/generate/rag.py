import logging

from openai import OpenAI
from qdrant_client import QdrantClient

from docquery.config import Settings, get_settings
from docquery.retrieve.hybrid import retrieve
from docquery.retrieve.reranker import rerank

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a technical documentation assistant. Answer the user's question using \
only the provided context passages. Cite sources inline as [1], [2], etc., \
where the number corresponds to the passage number. If the context does not \
contain enough information to answer, say so clearly.\
"""


def generate_answer(
    query: str,
    contexts: list[dict],
    settings: Settings,
    openai_client: OpenAI,
) -> dict:
    """Call the LLM with ranked context passages and return the answer with sources."""
    numbered = "\n\n".join(
        f"[{i + 1}] (source: {ctx['source']})\n{ctx['text']}"
        for i, ctx in enumerate(contexts)
    )
    user_message = f"Context:\n{numbered}\n\nQuestion: {query}"

    response = openai_client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )

    answer = response.choices[0].message.content or ""
    sources = [
        {
            "index": i + 1,
            "source": ctx["source"],
            "chunk_index": ctx["chunk_index"],
            "score": ctx["score"],
            "text": ctx["text"],
        }
        for i, ctx in enumerate(contexts)
    ]

    return {
        "answer": answer,
        "sources": sources,
        "model": response.model,
    }


def query_pipeline(query: str, settings: Settings | None = None) -> dict:
    """Full query pipeline: retrieve → rerank → generate.

    Returns {"answer": str, "sources": list[dict], "query": str, "model": str}.
    """
    settings = settings or get_settings()
    qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    openai_client = OpenAI(api_key=settings.openai_api_key.get_secret_value() or None)

    points = retrieve(query, qdrant, settings)
    contexts = rerank(query, points, settings)
    logger.info(
        "Query: %r — retrieved %d points, reranked to %d",
        query,
        len(points),
        len(contexts),
    )
    if not contexts:
        return {
            "answer": (
                "No documents have been indexed yet. Please ingest documents first."
            ),
            "sources": [],
            "query": query,
            "model": settings.llm_model,
        }

    result = generate_answer(query, contexts, settings, openai_client)

    return {**result, "query": query}
