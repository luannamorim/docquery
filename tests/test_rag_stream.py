"""Streaming generation: citations first, then the answer.

The ordering is the point. Retrieval and reranking both finish before the LLM is
called, so the sources are already known when generation starts — a client can
show what it is about to answer from while the text is still arriving. That is
only true if the pipeline emits them first, which is what these pin.

The non-streaming query_pipeline is left alone: run_eval.py drives it, and the
RAGAS baseline must not start depending on a streaming path.
"""

from types import SimpleNamespace

import pytest

from docquery.config import Settings
from docquery.generate.rag import query_pipeline_stream


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test", "llm_model": "gpt-4o-mini"}
    defaults.update(overrides)
    return Settings(**defaults)


def _contexts():
    return [
        {
            "source": "docs/contracts/acme_supply_2024.md",
            "chunk_index": 0,
            "score": 0.9,
            "text": "O prazo de pagamento e de 30 dias.",
            "section": "Pagamento",
            "folders": ["contracts"],
        }
    ]


def _chunks(*texts: str):
    """Mimic the OpenAI streaming shape: deltas, then a usage-carrying final."""
    for text in texts:
        delta = SimpleNamespace(content=text)
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=None)],
            model="gpt-4o-mini",
            usage=None,
        )
    yield SimpleNamespace(
        choices=[
            SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")
        ],
        model="gpt-4o-mini",
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
    )


@pytest.fixture
def piped(monkeypatch):
    """Stub everything up to generation; the stream itself stays real."""
    from docquery.generate import rag

    monkeypatch.setattr(rag, "QdrantClient", lambda **kwargs: object())
    monkeypatch.setattr(rag, "retrieve", lambda *a, **k: [object()])
    monkeypatch.setattr(rag, "rerank", lambda *a, **k: _contexts())
    monkeypatch.setattr(rag, "expand_contexts", lambda contexts, *a, **k: contexts)

    def _client(**kwargs):
        client = SimpleNamespace()
        client.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: _chunks("O prazo ", "e de 30 dias [1].")
            )
        )
        return client

    monkeypatch.setattr(rag, "OpenAI", _client)


def test_sources_arrive_before_the_first_token(piped):
    events = list(query_pipeline_stream("Qual o prazo?", _settings()))

    kinds = [e["type"] for e in events]
    assert kinds[0] == "sources"
    assert kinds.index("sources") < kinds.index("token")


def test_the_text_arrives_in_pieces_and_reassembles(piped):
    events = list(query_pipeline_stream("Qual o prazo?", _settings()))

    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert len(tokens) > 1
    assert "".join(tokens) == "O prazo e de 30 dias [1]."


def test_the_final_event_carries_the_cost(piped):
    events = list(query_pipeline_stream("Qual o prazo?", _settings()))

    done = events[-1]
    assert done["type"] == "done"
    assert done["tokens_in"] == 100
    assert done["tokens_out"] == 50
    assert done["cost_usd"] == pytest.approx((100 * 0.15 + 50 * 0.60) / 1_000_000)
    # The full answer too, so the caller recording history does not have to
    # reassemble what it already streamed.
    assert done["answer"] == "O prazo e de 30 dias [1]."
