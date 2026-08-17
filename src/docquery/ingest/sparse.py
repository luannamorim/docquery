import hashlib
import re
from collections import Counter

VOCAB_SIZE = 30_000

#: The one tokenizer. Query side and index side must agree exactly or a term the
#: index holds is not the term the query asks for, so everything that tokenizes
#: for retrieval goes through `tokens` rather than repeating this pattern.
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Lowercase alphanumeric runs, in order, with repeats kept.

    No stemming and no stopword list: Qdrant's Modifier.IDF already discounts
    terms that appear everywhere, which is the job a stopword list would do
    worse.
    """
    return _TOKEN.findall(text.lower())


def _stable_hash(token: str) -> int:
    """Map a token to a stable integer index via MD5.

    Using MD5 rather than Python's hash() because hash() is randomized
    per-process, which would break the ingestion/query index alignment.
    """
    return int(hashlib.md5(token.encode()).hexdigest(), 16) % VOCAB_SIZE


def document_terms(source: str, folders: list[str]) -> str:
    """The document's own name and category, as terms BM25 can match.

    A contract names its parties once and then says "a CONTRATADA" for the rest
    of its length, so most of its chunks contain nothing that identifies which
    contract they belong to. Asking for "o prazo do contrato da CRK" then loses
    to a clause titled "DO PRAZO" in a different contract — it is the better
    match for every word the query has to offer.

    Only the file name and the folder facets. The rest of a path — the host, the
    site, the drive — is identical for every document in a tenant, so indexing
    it would add terms that separate nothing while diluting the ones that do.

    Added to the lexical index only. The passage stored, shown in a citation and
    sent to the model stays exactly what the document says; inventing text a
    document does not contain is how a citation stops meaning anything.
    """
    name = source.replace("\\", "/").split("/")[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", stem) if p]
    return " ".join([*parts, *folders]).lower()


def sparse_vector(text: str) -> tuple[list[int], list[float]]:
    """Compute a BM25-style sparse term-frequency vector.

    Tokenizes text, counts term frequencies, and maps tokens to integer
    indices. Qdrant's Modifier.IDF handles inverse document frequency
    weighting at query time, so only raw TF is stored here.

    Returns (indices, values) for SparseVector(indices=..., values=...).
    """
    counted = tokens(text)
    if not counted:
        return [], []
    counts = Counter(counted)
    merged: dict[int, float] = {}
    for token, count in counts.items():
        idx = _stable_hash(token)
        merged[idx] = merged.get(idx, 0.0) + float(count)
    return list(merged.keys()), list(merged.values())
