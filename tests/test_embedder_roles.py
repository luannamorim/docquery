"""The e5 prefix protocol lives inside the embedder, never at a call site.

e5-family models are trained with asymmetric "query: "/"passage: " markers;
omitting them on either side silently degrades retrieval — the search still
returns results, just worse ones, which is the worst way to fail. Callers
therefore state a role and the embedder decides the prefix, conditioned on
the model, so a model swap cannot desynchronize the two sides.

Everything here is hermetic: `_get_model` is swapped for a fake whose
vectors are deterministic in the input string, so a prefixed input IS a
different vector — no model download, same asymmetry.
"""

import hashlib

import numpy as np
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.retrieve import embedder
from docquery.retrieve.embedder import embed_texts

E5 = "intfloat/multilingual-e5-base"
DIM = 8


class FakeModel:
    """Deterministic in the input text: different string, different vector."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, texts, **_kwargs) -> np.ndarray:
        self.calls.append(list(texts))
        return np.stack([self._vector(t) for t in texts])

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode()).digest()
        raw = np.frombuffer(digest[:DIM], dtype=np.uint8).astype(np.float32) + 1.0
        return raw / np.linalg.norm(raw)


@pytest.fixture
def fake_model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(embedder, "_get_model", lambda name: fake)
    return fake


def test_the_same_text_embeds_differently_as_query_and_as_passage(fake_model):
    """The asymmetry is the point: bare-encoding both sides is the silent bug."""
    settings = Settings(embedding_model=E5, embedding_dimension=768)

    q = embed_texts(["quitação do contrato"], settings=settings, role="query")
    p = embed_texts(["quitação do contrato"], settings=settings, role="passage")

    assert not np.array_equal(q, p)


def test_prefix_models_get_typed_prefixes(fake_model):
    settings = Settings(embedding_model=E5, embedding_dimension=768)

    embed_texts(["quitação"], settings=settings, role="query")
    embed_texts(["quitação"], settings=settings, role="passage")

    assert fake_model.calls == [["query: quitação"], ["passage: quitação"]]


def test_prefix_free_models_embed_identically_for_both_roles(fake_model):
    """The default model has no protocol: both roles encode the bare text,
    byte-identical to the behavior before roles existed."""
    settings = Settings()  # all-MiniLM-L6-v2

    q = embed_texts(["quitação"], settings=settings, role="query")
    p = embed_texts(["quitação"], settings=settings, role="passage")

    assert np.array_equal(q, p)
    assert fake_model.calls == [["quitação"], ["quitação"]]


def test_query_and_passage_sides_route_their_roles(monkeypatch):
    """hybrid embeds the question as a query; the pipeline embeds chunks as
    passages. Captured at embed_texts so the routing itself is what's pinned."""
    from docquery.ingest import pipeline
    from docquery.ingest.chunker import Chunk
    from docquery.retrieve import hybrid

    roles: list[str] = []

    def spy(texts, settings=None, batch_size=32, role="passage"):
        roles.append(role)
        return np.tile(np.eye(1, DIM, dtype=np.float32), (len(texts), 1))

    monkeypatch.setattr(pipeline, "embed_texts", spy)
    monkeypatch.setattr(hybrid, "embed_texts", spy)

    client = _in_memory_client()
    settings = Settings(qdrant_collection=COLLECTION)
    pipeline.ingest_chunks(
        [Chunk(text="pagamento", metadata={"source": "a.pdf", "chunk_index": 0})],
        client,
        settings,
    )
    hybrid.retrieve("como pagar", client, settings=settings)

    assert roles == ["passage", "query"]


COLLECTION = "test_embedder_roles"


def _in_memory_client() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    return client


def test_roles_route_end_to_end_through_ingest_and_query(fake_model):
    """Hermetic end to end: the fake is deterministic, so the passage whose
    prefixed text hashes nearest the prefixed question wins — proving the real
    embed_texts carried the right role through both pipelines."""
    from docquery.ingest import pipeline
    from docquery.ingest.chunker import Chunk
    from docquery.retrieve import hybrid

    question = "como faço a quitação do contrato?"
    winner = "A quitação do contrato exige boleto pago."

    # Rig the geometry: the winning passage shares the question's vector.
    target = FakeModel._vector(f"query: {question}")
    original = FakeModel._vector

    def rigged(text: str) -> np.ndarray:
        if text == f"passage: {winner}":
            return target
        return original(text)

    fake_model._vector = rigged

    client = _in_memory_client()
    settings = Settings(
        embedding_model=E5, embedding_dimension=768, qdrant_collection=COLLECTION
    )
    chunks = [
        Chunk(text=winner, metadata={"source": "m.pdf", "chunk_index": 0}),
        Chunk(
            text="Reemissão de boleto leva 2 dias.",
            metadata={"source": "m.pdf", "chunk_index": 1},
        ),
        Chunk(
            text="Desbloqueio de conta abre chamado.",
            metadata={"source": "m.pdf", "chunk_index": 2},
        ),
    ]
    pipeline.ingest_chunks(chunks, client, settings)
    points = hybrid.retrieve(question, client, settings=settings)

    passage_calls = [c for c in fake_model.calls if len(c) == 3]
    query_calls = [c for c in fake_model.calls if len(c) == 1]
    assert passage_calls == [[f"passage: {c.text}" for c in chunks]]
    assert query_calls == [[f"query: {question}"]]
    assert points[0].payload["text"] == winner
