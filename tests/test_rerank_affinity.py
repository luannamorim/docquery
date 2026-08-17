"""The named document gets the slots, and it gets them before the cut.

Promoting after `reranker_top_k` would be too late: with RERANKER_TOP_K=5 and 20
candidates, the right contract's clause can be ranked 9th by a cross-encoder
that cannot see which contract it is, and never reach the list there is
something to promote in. So the partition happens on the full scored set and the
truncation happens after it.

The threshold still applies first. A preference decides *which* of the passages
worth sending gets a slot, never whether a passage nobody should read gets one.
"""

import pytest
from qdrant_client.models import ScoredPoint

from docquery.config import Settings
from docquery.retrieve.reranker import rerank


def _point(source: str, index: int, text: str) -> ScoredPoint:
    return ScoredPoint(
        id=index,
        version=0,
        score=0.5,
        payload={
            "text": text,
            "source": source,
            "chunk_index": index,
            "section": "",
            "folders": ["contracts"],
        },
    )


class _StubEncoder:
    """Scores by position in `order`, highest first, mimicking CrossEncoder.rank."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def rank(self, query, texts, top_k=None, return_documents=False):
        ranked = [{"corpus_id": i, "score": self._scores[i]} for i in range(len(texts))]
        ranked.sort(key=lambda r: r["score"], reverse=True)
        return ranked[:top_k] if top_k else ranked


@pytest.fixture
def stub_encoder(monkeypatch):
    def _install(scores: list[float]):
        encoder = _StubEncoder(scores)
        monkeypatch.setattr(
            "docquery.retrieve.reranker._get_reranker", lambda _name: encoder
        )
        return encoder

    return _install


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test", "reranker_top_k": 2}
    defaults.update(overrides)
    return Settings(**defaults)


def test_without_a_preference_the_cross_encoder_order_stands(stub_encoder):
    stub_encoder([1.0, 5.0, 3.0])
    points = [
        _point("db1_2023.pdf", 0, "a"),
        _point("db1_2023.pdf", 1, "b"),
        _point("crk_2025.pdf", 2, "c"),
    ]

    out = rerank("prazo", points, _settings())

    assert [c["chunk_index"] for c in out] == [1, 2]


def test_the_named_document_takes_the_slots(stub_encoder):
    """The exact failure: the cross-encoder likes db1's "DO PRAZO" clause best,
    but the question asked about CRK."""
    stub_encoder([9.0, 8.0, 3.0])
    points = [
        _point("db1_2023.pdf", 0, "DO PRAZO ..."),
        _point("db1_2023.pdf", 1, "DO PRAZO ..."),
        _point("crk_2025.pdf", 2, "prazo indeterminado"),
    ]

    out = rerank("prazo da crk", points, _settings(), prefer_sources={"crk_2025.pdf"})

    assert out[0]["source"] == "crk_2025.pdf"


def test_a_promoted_passage_survives_a_cut_it_would_have_missed(stub_encoder):
    """Promotion happens before the truncation, not after it: ranked 3rd of 3
    with room for 2, the named passage still makes the list."""
    stub_encoder([9.0, 8.0, 3.0])
    points = [
        _point("db1_2023.pdf", 0, "a"),
        _point("db1_2023.pdf", 1, "b"),
        _point("crk_2025.pdf", 2, "c"),
    ]

    out = rerank("prazo da crk", points, _settings(), prefer_sources={"crk_2025.pdf"})

    assert [c["chunk_index"] for c in out] == [2, 0]


def test_the_rest_keeps_its_relative_order(stub_encoder):
    stub_encoder([9.0, 8.0, 3.0])
    points = [
        _point("db1_2023.pdf", 0, "a"),
        _point("db1_2023.pdf", 1, "b"),
        _point("crk_2025.pdf", 2, "c"),
    ]

    out = rerank(
        "prazo da crk",
        points,
        _settings(reranker_top_k=3),
        prefer_sources={"crk_2025.pdf"},
    )

    assert [c["chunk_index"] for c in out] == [2, 0, 1]


def test_the_threshold_still_cuts_a_promoted_passage(stub_encoder):
    """A preference reorders what is worth reading; it does not make a passage
    worth reading. Otherwise naming a document would drag its worst chunk into
    the prompt ahead of a good one from elsewhere."""
    stub_encoder([9.0, 8.0, -20.0])
    points = [
        _point("db1_2023.pdf", 0, "a"),
        _point("db1_2023.pdf", 1, "b"),
        _point("crk_2025.pdf", 2, "c"),
    ]

    out = rerank("prazo da crk", points, _settings(), prefer_sources={"crk_2025.pdf"})

    assert [c["source"] for c in out] == ["db1_2023.pdf", "db1_2023.pdf"]


def test_the_ablation_path_is_untouched(stub_encoder):
    """reranker_top_k <= 0 is the genuine "reranker off" path. A preference must
    not quietly turn it into a reranked one."""
    points = [_point("db1_2023.pdf", 0, "a"), _point("crk_2025.pdf", 1, "b")]

    out = rerank(
        "prazo da crk",
        points,
        _settings(reranker_top_k=0),
        prefer_sources={"crk_2025.pdf"},
    )

    assert [c["chunk_index"] for c in out] == [0, 1]


def test_no_points_stays_empty(stub_encoder):
    assert (
        rerank("prazo da crk", [], _settings(), prefer_sources={"crk_2025.pdf"}) == []
    )
