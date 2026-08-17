"""Which document, if any, the question actually names.

`document_terms` fixed half of this. A contract names its parties once and then
says "a CONTRATADA" for the rest of its length, so most of its chunks hold
nothing saying which contract they belong to — putting the file name into the
lexical index is what lets retrieval find them at all.

The cross-encoder then discards it. `rerank` scores the query against
`payload["text"]` alone, and that text still never says "CRK", so a clause
titled "DO PRAZO" in a *different* contract remains the better match for every
word the reranker is allowed to see. The lexical boost survives the RRF fusion
and dies at the rerank.

So the document a question names is read here, from the same terms the index
already matches on, and handed to `rerank` as the passages that get the slots
first. Deliberately not a filter: naming a document is a strong preference, not
a claim that no other document can be relevant, and a hard filter on a guess
about the caller's intent is how "prazo da CRK" stops being able to say
"...and the master agreement it amends".

Nothing is invented for this. It reuses `document_terms` and `tokens`, so what
counts as a document's name is one definition, not two that can drift.

Known limitation, inherited on purpose: `tokens` matches `[a-z0-9]+`, so an
accented word breaks apart ("política" -> "pol", "tica") and cannot match the
unaccented stem of a file name. A document named `politica_x.pdf` is therefore
not named by a question that spells it "política". Sharing the flaw with the
lexical index is the lesser evil — a tokenizer that only this module folded
accents would match terms the index does not hold, which is a silent wrong
answer rather than a visible miss.
"""

import logging
from collections.abc import Mapping

from qdrant_client.models import ScoredPoint

from docquery.ingest.sparse import document_terms, tokens

logger = logging.getLogger(__name__)

#: Two-character terms are dropped. A file named "contrato_de_prestacao.pdf"
#: offers "de", which appears in almost every Portuguese question — matching on
#: it would make that one document the preferred answer to everything.
_MIN_TERM_LENGTH = 3


def documents_in(points: list[ScoredPoint]) -> dict[str, list[str]]:
    """The distinct documents a retrieval touched, as source -> folders.

    Sources are read from the payload here rather than in the caller, next to
    the other payload reader in this package. A point with no source is dropped:
    "" is not a document, and letting it through would make `document_terms`
    describe the empty string.
    """
    documents: dict[str, list[str]] = {}
    for point in points:
        payload = point.payload or {}
        source = str(payload.get("source", ""))
        if source:
            documents[source] = list(payload.get("folders") or [])
    return documents


def named_sources(query: str, documents: Mapping[str, list[str]]) -> set[str]:
    """The sources in `documents` whose own name the query mentions.

    `documents` maps a source to its folders — exactly the two things
    `document_terms` reads. Returns an empty set when the query names none of
    them, which is the common case and the one that must stay cheap: the caller
    then changes nothing about the ranking it already had.

    A term carried by *every* candidate is ignored. "contracts" shared by the
    whole candidate set separates none of it, and reordering on a signal with no
    information in it would demote passages the cross-encoder ranked on merit.
    With a single candidate there is nothing to separate it from, so the rule
    does not apply — and the preference it yields is a no-op anyway.
    """
    if not documents:
        return set()

    terms = {
        source: {
            t
            for t in tokens(document_terms(source, folders))
            if len(t) >= _MIN_TERM_LENGTH
        }
        for source, folders in documents.items()
    }

    if len(terms) > 1:
        shared = set.intersection(*terms.values())
        terms = {source: t - shared for source, t in terms.items()}

    asked = set(tokens(query))
    return {source for source, t in terms.items() if t & asked}
