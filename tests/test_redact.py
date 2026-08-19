"""PII must become a typed placeholder before anything persists.

The operational manuals carry real customer data — CPF, CNPJ, e-mail,
phone — in native text and inside screenshots that OCR recovers. Anything
that reaches the Qdrant payload comes back out in citations to every user
of the sector, which is an LGPD exposure. Redaction replaces, never
removes: a passage with a silent hole would still read as the document's
own words, and a citation must stay legible.

Validation guards the other direction: a 9-12 digit contract number that
merely looks like a CPF must survive untouched, because a redacted
contract number is a silent retrieval loss.
"""

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    SparseVectorParams,
    VectorParams,
)

from docquery.config import Settings
from docquery.ingest.chunker import Chunk
from docquery.ingest.redact import redact_chunks, redact_text


def test_a_valid_cpf_becomes_a_typed_placeholder():
    assert redact_text("CPF do cliente: 529.982.247-25") == "CPF do cliente: [CPF]"
    assert redact_text("CPF 52998224725 informado") == "CPF [CPF] informado"


def test_a_cpf_with_a_wrong_check_digit_is_left_alone():
    assert "529.982.247-26" in redact_text("valor 529.982.247-26")


def test_repeated_digit_cpfs_are_invalid_despite_passing_the_dv():
    assert "111.111.111-11" in redact_text("teste 111.111.111-11")


def test_contract_numbers_of_nine_to_twelve_digits_are_not_cpfs():
    """An 11-digit slice of a longer run must never match (lookaround guards)."""
    text = "contrato 123456789 e apólice 202400012345"
    assert redact_text(text) == text


def test_cnpj_numeric_and_alphanumeric_are_both_detected():
    assert redact_text("CNPJ 11.222.333/0001-81") == "CNPJ [CNPJ]"
    assert redact_text("CNPJ 11222333000181") == "CNPJ [CNPJ]"

    # The alphanumeric format (Receita rollout): build a DV-valid example with
    # the module's own check-digit routine, then require detection.
    from docquery.ingest.redact import _cnpj_dvs

    base = "12ABC34501DE"
    dv1, dv2 = _cnpj_dvs(base)
    formatted = f"{base[:2]}.{base[2:5]}.{base[5:8]}/{base[8:12]}-{dv1}{dv2}"
    assert redact_text(f"registro {formatted}") == "registro [CNPJ]"


def test_a_cnpj_with_a_wrong_check_digit_is_left_alone():
    assert "11.222.333/0001-82" in redact_text("valor 11.222.333/0001-82")


def test_emails_become_placeholders():
    assert (
        redact_text("mande para joao.silva+x@sub.empresa.com.br")
        == "mande para [EMAIL]"
    )


def test_br_phones_with_ddd_or_country_code_are_detected():
    for phone in ["+55 (11) 91234-5678", "(65) 3644-1234", "98765-4321"]:
        assert "[TELEFONE]" in redact_text(f"ligar {phone} hoje"), phone


def test_dates_and_year_ranges_are_not_phones():
    for text in ["vigência 12/08/2026", "período 2020-2024"]:
        assert redact_text(text) == text


def test_content_derived_metadata_is_redacted_but_paths_are_not():
    """source/folders are operator-controlled paths; section/title/tags carry
    document content and can leak the same PII the text does."""
    chunk = Chunk(
        text="contato 529.982.247-25",
        metadata={
            "section": "Cliente 529.982.247-25",
            "title": "Ficha 529.982.247-25",
            "tags": ["529.982.247-25", "cadastro"],
            "source": "docs/52998224725/ficha.pdf",
            "folders": ["52998224725"],
        },
    )
    [redacted] = redact_chunks([chunk], Settings(pii_redaction_enabled=True))

    assert redacted.text == "contato [CPF]"
    assert redacted.metadata["section"] == "Cliente [CPF]"
    assert redacted.metadata["title"] == "Ficha [CPF]"
    assert redacted.metadata["tags"] == ["[CPF]", "cadastro"]
    assert redacted.metadata["source"] == "docs/52998224725/ficha.pdf"
    assert redacted.metadata["folders"] == ["52998224725"]


def test_flag_off_returns_the_same_chunk_objects():
    """Off means off: identity, not a copy — byte-identical behavior."""
    chunks = [Chunk(text="CPF 529.982.247-25")]
    assert redact_chunks(chunks, Settings(pii_redaction_enabled=False)) is chunks


# --- Integration: nothing PII-shaped may reach a Qdrant payload -------------

DIM = 8
COLLECTION = "test_redact"


def _fake_dense(texts: list[str], **_kwargs) -> np.ndarray:
    """One arbitrary unit vector per chunk; retrieval quality is not the point."""
    return np.tile(np.eye(1, DIM, dtype=np.float32), (len(texts), 1))


def _in_memory_client() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(modifier=Modifier.IDF)},
    )
    return client


def test_no_upserted_payload_contains_a_valid_cpf(monkeypatch):
    """The seam lives in ingest_chunks — the only door to Qdrant — so an
    unredacted chunk cannot reach a payload no matter which caller built it."""
    from docquery.ingest import pipeline
    from docquery.ingest.redact import _CPF_RE, _valid_cpf

    monkeypatch.setattr(pipeline, "embed_texts", _fake_dense)
    client = _in_memory_client()
    settings = Settings(
        pii_redaction_enabled=True,
        qdrant_collection=COLLECTION,
        embedding_dimension=DIM,
    )
    chunks = [
        Chunk(
            text="Cliente CPF 529.982.247-25, contato (65) 99123-4567",
            metadata={
                "source": "docs/manual.pdf",
                "chunk_index": 0,
                "section": "Cadastro 529.982.247-25",
                "tags": ["52998224725"],
            },
        ),
        Chunk(
            text="Titular 111.444.777-35 encerrou a conta",
            metadata={"source": "docs/manual.pdf", "chunk_index": 1},
        ),
    ]

    pipeline.ingest_chunks(chunks, client, settings)

    points, _ = client.scroll(collection_name=COLLECTION, limit=100, with_payload=True)
    assert points, "expected upserted points"
    for point in points:
        for value in (point.payload or {}).values():
            strings = value if isinstance(value, list) else [value]
            for item in strings:
                if not isinstance(item, str):
                    continue
                hits = [
                    m.group(0)
                    for m in _CPF_RE.finditer(item)
                    if _valid_cpf("".join(c for c in m.group(0) if c.isdigit()))
                ]
                assert not hits, f"valid CPF leaked into payload: {hits}"
    texts = [p.payload["text"] for p in points]
    assert any("[CPF]" in t for t in texts), "replacement, not removal"
