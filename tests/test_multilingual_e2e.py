"""Portuguese retrieval with the real multilingual-e5-base, end to end.

A broken asymmetric protocol breaks search silently — results still come
back, just worse ones — so this test uses the real model and fails loud.
Opt-in like the Docling and MySQL suites: the model is ~1.1GB of weights.

    DOCQUERY_MULTILINGUAL_E2E=1 uv run pytest -m multilingual
"""

import os

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.ingest.chunker import Chunk

pytestmark = [
    pytest.mark.multilingual,
    pytest.mark.skipif(
        os.environ.get("DOCQUERY_MULTILINGUAL_E2E") != "1",
        reason="set DOCQUERY_MULTILINGUAL_E2E=1 to run the e5 retrieval test",
    ),
]

COLLECTION = "test_multilingual_e2e"
DIM = 768


def test_a_portuguese_query_retrieves_the_right_chunk_first():
    from docquery.ingest import pipeline
    from docquery.retrieve import hybrid

    settings = Settings(
        embedding_model="intfloat/multilingual-e5-base",
        embedding_dimension=DIM,
        qdrant_collection=COLLECTION,
    )
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    chunks = [
        Chunk(
            text=(
                "Para encerrar o financiamento antes do prazo, o cliente "
                "solicita o boleto de liquidação e paga o saldo devedor."
            ),
            metadata={"source": "docs/manual.pdf", "chunk_index": 0},
        ),
        Chunk(
            text=(
                "A segunda via da fatura mensal é emitida pelo portal "
                "na aba de documentos."
            ),
            metadata={"source": "docs/manual.pdf", "chunk_index": 1},
        ),
        Chunk(
            text=(
                "O desbloqueio de acesso ao aplicativo exige a abertura "
                "de um chamado com a equipe de suporte."
            ),
            metadata={"source": "docs/manual.pdf", "chunk_index": 2},
        ),
    ]

    pipeline.ingest_chunks(chunks, client, settings)
    points = hybrid.retrieve(
        "como faço a quitação do contrato?", client, settings=settings
    )

    # The question shares no keyword with the winning chunk ("quitação" vs
    # "liquidação"/"encerrar") — semantics, not lexical overlap, must win,
    # which is exactly what the prefix protocol buys.
    assert points, "expected retrieval results"
    assert points[0].payload["chunk_index"] == 0
