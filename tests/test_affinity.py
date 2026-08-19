"""When a question names a document, that document should win its own slots.

`document_terms` put the file name into the lexical index, so retrieval finds
the right contract's chunks. The cross-encoder then throws that away: it scores
`payload["text"]` alone, and the passage never says "CRK". So "qual o prazo do
contrato da CRK" still loses to a clause titled "DO PRAZO" in a different
contract — the better match for every word the reranker is allowed to see.

These pin the missing half: reading the document a question names out of the
same terms the index already matches on.
"""

from docquery.retrieve.affinity import named_sources


def test_the_document_the_question_names_is_the_one_returned():
    documents = {
        "data/contracts/crk_2025.pdf": ["contracts"],
        "data/contracts/db1_2023.pdf": ["contracts"],
    }

    named = named_sources("qual o prazo do contrato da CRK e o valor", documents)

    assert named == {"data/contracts/crk_2025.pdf"}


def test_a_question_that_names_nothing_names_nothing():
    """The common case, and the one that must stay untouched: no document in
    the candidate set is preferred, so the cross-encoder's order stands."""
    documents = {
        "data/contracts/crk_2025.pdf": ["contracts"],
        "data/contracts/db1_2023.pdf": ["contracts"],
    }

    assert named_sources("qual o prazo de pagamento", documents) == set()


def test_a_term_every_candidate_shares_names_nothing():
    """ "quais contratos temos" must not "name" all of them: a term carried by
    every candidate separates none of them, and treating it as a match would
    reorder the whole list on a signal with no information in it."""
    documents = {
        "data/contracts/crk_2025.pdf": ["contracts"],
        "data/contracts/db1_2023.pdf": ["contracts"],
    }

    assert named_sources("what contracts do we have", documents) == set()


def test_a_shared_term_still_does_not_hide_a_discriminating_one():
    documents = {
        "data/contracts/crk_2025.pdf": ["contracts"],
        "data/contracts/db1_2023.pdf": ["contracts"],
    }

    named = named_sources("contracts signed with CRK", documents)

    assert named == {"data/contracts/crk_2025.pdf"}


def test_short_tokens_are_ignored():
    """A file named "contrato_de_prestacao.pdf" offers "de", which appears in
    almost every Portuguese question. Matching on it would make that document
    the answer to everything."""
    documents = {
        "data/contracts/contrato_de_prestacao.pdf": [],
        "data/contracts/crk_2025.pdf": [],
    }

    named = named_sources("qual o prazo de vigencia", documents)

    assert named == set()


def test_a_question_with_accents_names_an_unaccented_file():
    """ "política de férias" must reach politica_ferias.pdf: the shared
    tokenizer folds accents, so the question's terms are the file's terms."""
    documents = {
        "docs/rh/politica_ferias.pdf": ["rh"],
        "docs/rh/beneficios.pdf": ["rh"],
    }

    named = named_sources("qual a política de férias", documents)

    assert named == {"docs/rh/politica_ferias.pdf"}


def test_a_year_in_the_question_selects_by_year():
    documents = {
        "data/contracts/crk_2025.pdf": ["contracts"],
        "data/contracts/crk_2019.pdf": ["contracts"],
    }

    named = named_sources("o que mudou no contrato de 2025", documents)

    assert named == {"data/contracts/crk_2025.pdf"}


def test_the_match_is_case_blind():
    """Same tokenizer as the sparse vector, so "CRK" and "crk" are one term —
    and sharing the tokenizer is the point: a query tokenized differently from
    the index would match terms the index does not hold."""
    documents = {"data/contracts/CRK_2025.pdf": []}

    assert named_sources("prazo da crk", documents) == {"data/contracts/CRK_2025.pdf"}


def test_a_remote_uri_is_named_by_its_file_name():
    documents = {
        "sharepoint://contoso.sharepoint.com/sites/Corp/rh/ferias_2024.docx": ["rh"],
        "sharepoint://contoso.sharepoint.com/sites/Corp/rh/ponto_2024.docx": ["rh"],
    }

    named = named_sources("politica de ferias", documents)

    assert named == {
        "sharepoint://contoso.sharepoint.com/sites/Corp/rh/ferias_2024.docx"
    }


def test_the_host_and_site_never_name_anything():
    """They are identical for every document in a tenant, so they are dropped as
    non-discriminating — the same reason document_terms never indexes them."""
    documents = {
        "sharepoint://contoso.sharepoint.com/sites/Corp/rh/ferias_2024.docx": ["rh"],
        "sharepoint://contoso.sharepoint.com/sites/Corp/rh/ponto_2024.docx": ["rh"],
    }

    assert named_sources("contoso sharepoint corp", documents) == set()


def test_no_candidates_names_nothing():
    assert named_sources("prazo da crk", {}) == set()


def test_a_single_candidate_can_still_be_named():
    """With one document in play nothing is "shared by every candidate" in a way
    that separates — but the question naming it is still a real signal, and the
    partition it produces is simply a no-op."""
    documents = {"data/contracts/crk_2025.pdf": ["contracts"]}

    assert named_sources("prazo da crk", documents) == {"data/contracts/crk_2025.pdf"}
