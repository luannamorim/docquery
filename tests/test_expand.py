from unittest.mock import MagicMock

from docquery.config import Settings
from docquery.retrieve.expand import expand_contexts


def _settings(**overrides) -> Settings:
    return Settings(**{"openai_api_key": "test", **overrides})


def _make_point(chunk_index: int, text: str, source: str = "doc.pdf") -> MagicMock:
    point = MagicMock()
    point.payload = {"chunk_index": chunk_index, "text": text, "source": source}
    return point


def test_expand_merges_neighbors() -> None:
    ctx = {"source": "doc.pdf", "chunk_index": 5, "score": 1.0, "text": "original"}
    points = [
        _make_point(4, "before"),
        _make_point(5, "target"),
        _make_point(6, "after"),
    ]
    client = MagicMock()
    client.scroll.return_value = (points, None)
    result = expand_contexts([ctx], client, _settings(context_expansion_window=1))
    assert len(result) == 1
    assert result[0]["text"] == "before\ntarget\nafter"


def test_expand_window_zero_is_noop() -> None:
    ctx = {"source": "doc.pdf", "chunk_index": 5, "score": 1.0, "text": "original"}
    client = MagicMock()
    result = expand_contexts([ctx], client, _settings(context_expansion_window=0))
    assert result == [ctx]
    client.scroll.assert_not_called()


def test_expand_deduplicates_overlapping_windows() -> None:
    contexts = [
        {"source": "doc.pdf", "chunk_index": 5, "score": 2.0, "text": "a"},
        {"source": "doc.pdf", "chunk_index": 6, "score": 1.0, "text": "b"},
    ]
    points = [
        _make_point(4, "p4"),
        _make_point(5, "p5"),
        _make_point(6, "p6"),
    ]
    client = MagicMock()
    client.scroll.return_value = (points, None)
    result = expand_contexts(contexts, client, _settings(context_expansion_window=1))
    # idx=5 window=[4,6], idx=6 window=[5,7] — different windows, both kept
    assert len(result) == 2
    assert client.scroll.call_count == 2


def test_expand_same_window_not_duplicated() -> None:
    contexts = [
        {"source": "doc.pdf", "chunk_index": 5, "score": 2.0, "text": "a"},
        {"source": "doc.pdf", "chunk_index": 5, "score": 1.5, "text": "a"},
    ]
    client = MagicMock()
    client.scroll.return_value = ([_make_point(5, "p5")], None)
    result = expand_contexts(contexts, client, _settings(context_expansion_window=1))
    assert len(result) == 1
    assert client.scroll.call_count == 1
