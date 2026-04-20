import hashlib
import re
from collections import Counter

VOCAB_SIZE = 30_000


def _stable_hash(token: str) -> int:
    """Map a token to a stable integer index via MD5.

    Using MD5 rather than Python's hash() because hash() is randomized
    per-process, which would break the ingestion/query index alignment.
    """
    return int(hashlib.md5(token.encode()).hexdigest(), 16) % VOCAB_SIZE


def sparse_vector(text: str) -> tuple[list[int], list[float]]:
    """Compute a BM25-style sparse term-frequency vector.

    Tokenizes text, counts term frequencies, and maps tokens to integer
    indices. Qdrant's Modifier.IDF handles inverse document frequency
    weighting at query time, so only raw TF is stored here.

    Returns (indices, values) for SparseVector(indices=..., values=...).
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        return [], []
    counts = Counter(tokens)
    merged: dict[int, float] = {}
    for token, count in counts.items():
        idx = _stable_hash(token)
        merged[idx] = merged.get(idx, 0.0) + float(count)
    return list(merged.keys()), list(merged.values())
