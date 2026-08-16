"""Retrieval across the parts of a compound question.

Splitting the question is half the fix. The dilution happens in the
cross-encoder, which scores each candidate against whatever query it is handed —
so every part has to be reranked against *itself*. Rerank the union against the
original compound question and the scores collapse exactly as before, split or
no split.

These pin the merge: each part contributes its own best passages, the same
passage found twice is kept once, and the parts alternate so a truncated context
still carries both intents.
"""

from types import SimpleNamespace

from docquery.config import Settings
from docquery.generate.rag import merge_by_intent


def _ctx(source: str, index: int, score: float) -> dict:
    return {"source": source, "chunk_index": index, "score": score, "text": "x"}


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test"}
    defaults.update(overrides)
    return Settings(**defaults)


def test_a_single_part_passes_straight_through():
    per_part = [[_ctx("a.pdf", 1, 4.0), _ctx("a.pdf", 2, 3.0)]]

    assert merge_by_intent(per_part, _settings()) == per_part[0]


def test_both_intents_survive_the_merge():
    prazo = [_ctx("crk.pdf", 10, 4.8), _ctx("crk.pdf", 11, 4.1)]
    valor = [_ctx("crk.pdf", 40, 5.5), _ctx("crk.pdf", 41, 3.9)]

    merged = merge_by_intent([prazo, valor], _settings())

    assert {c["chunk_index"] for c in merged} == {10, 11, 40, 41}


def test_the_parts_alternate():
    """Interleaved, not concatenated: if the context is cut short, a reader
    still gets something from each question rather than all of the first."""
    prazo = [_ctx("crk.pdf", 10, 4.8), _ctx("crk.pdf", 11, 4.1)]
    valor = [_ctx("crk.pdf", 40, 5.5), _ctx("crk.pdf", 41, 3.9)]

    merged = merge_by_intent([prazo, valor], _settings())

    assert [c["chunk_index"] for c in merged] == [10, 40, 11, 41]


def test_a_passage_found_by_both_parts_appears_once():
    shared = _ctx("crk.pdf", 10, 4.8)
    merged = merge_by_intent([[shared], [dict(shared)]], _settings())

    assert len(merged) == 1


def test_an_empty_part_does_not_break_the_alternation():
    """A part can retrieve nothing — its sector may reach no matching passage."""
    merged = merge_by_intent(
        [[_ctx("a.pdf", 1, 4.0)], [], [_ctx("b.pdf", 2, 3.0)]], _settings()
    )

    assert [c["source"] for c in merged] == ["a.pdf", "b.pdf"]


def test_the_total_is_bounded():
    """Three parts of five would otherwise triple the prompt."""
    parts = [[_ctx(f"{p}.pdf", i, 1.0) for i in range(5)] for p in "abc"]

    merged = merge_by_intent(parts, _settings(query_decompose_max_contexts=8))

    assert len(merged) == 8


def test_the_bound_keeps_every_intent_represented():
    """Truncating a concatenation would drop the last question entirely."""
    parts = [[_ctx(f"{p}.pdf", i, 1.0) for i in range(5)] for p in "abc"]

    merged = merge_by_intent(parts, _settings(query_decompose_max_contexts=4))

    assert {c["source"] for c in merged} == {"a.pdf", "b.pdf", "c.pdf"}


def test_the_shape_matches_what_generation_expects():
    """Contexts flow straight into _fmt_context, which reads these keys."""
    merged = merge_by_intent([[_ctx("a.pdf", 1, 4.0)]], _settings())

    assert isinstance(merged[0], dict)
    assert {"source", "chunk_index", "score", "text"} <= set(merged[0])
    assert not isinstance(merged[0], SimpleNamespace)
