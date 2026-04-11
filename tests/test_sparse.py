from docquery.ingest.sparse import VOCAB_SIZE, _stable_hash, sparse_vector


def test_sparse_vector_basic() -> None:
    indices, values = sparse_vector("hello world hello")
    assert len(indices) == 2  # two unique tokens
    assert len(values) == 2
    # "hello" appears twice
    hello_idx = indices[0] if values[0] == 2.0 else indices[1]
    world_idx = indices[1] if values[0] == 2.0 else indices[0]
    assert hello_idx != world_idx


def test_sparse_vector_empty() -> None:
    indices, values = sparse_vector("")
    assert indices == []
    assert values == []


def test_sparse_vector_punctuation_only() -> None:
    indices, values = sparse_vector("!!! --- ???")
    assert indices == []
    assert values == []


def test_sparse_vector_case_insensitive() -> None:
    lower = sparse_vector("Hello")
    upper = sparse_vector("HELLO")
    assert lower == upper


def test_stable_hash_deterministic() -> None:
    assert _stable_hash("test") == _stable_hash("test")


def test_stable_hash_within_vocab_size() -> None:
    for word in ["hello", "world", "python", "qdrant", "embedding"]:
        assert 0 <= _stable_hash(word) < VOCAB_SIZE


def test_sparse_vector_term_frequencies() -> None:
    indices, values = sparse_vector("a b a b a")
    # "a" appears 3 times, "b" appears 2 times
    assert sorted(values) == [2.0, 3.0]


def test_sparse_vector_numbers_included() -> None:
    indices, values = sparse_vector("version 3 release 3")
    assert len(indices) == 3  # "version", "3", "release"
