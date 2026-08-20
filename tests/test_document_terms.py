"""Every chunk should be findable by the name of the document it came from.

A contract names its parties in the qualification clause and then says "a
CONTRATADA" for the rest of its length: in this corpus only 19% of one
contract's chunks contain the word "CRK" at all. Lexical retrieval could
therefore score just those 45 of 233, and "prazo do contrato da CRK" lost to
the clause literally titled "DO PRAZO" — in the *other* contract.

The fix belongs to the index, not to the text: the document's own name is added
to what BM25 matches on, while the passage stored, shown and sent to the model
stays exactly what the document says.
"""

from docquery.ingest.sparse import document_terms, sparse_vector


def test_the_file_name_becomes_searchable_terms():
    assert document_terms("data/contracts/crk_2025.pdf", []) == "crk 2025"


def test_folders_are_included_so_a_category_can_be_asked_for():
    """ "quais contratos temos" should reach chunks of every contract."""
    terms = document_terms("data/contracts/crk_2025.pdf", ["contracts"])

    assert "contracts" in terms
    assert "crk" in terms


def test_a_remote_uri_reduces_to_the_same_thing():
    terms = document_terms(
        "sharepoint://contoso.sharepoint.com/sites/Corp/Documentos/rh/ferias_2024.docx",
        ["rh"],
    )

    assert "ferias" in terms
    assert "2024" in terms
    # Not the host, the site or the drive: they are identical for every document
    # in the tenant, so indexing them would add a term that separates nothing.
    assert "contoso" not in terms
    assert "sharepoint" not in terms


def test_the_indexed_terms_reach_the_sparse_vector():
    """The point of all this: a chunk that never says "crk" still matches it."""
    passage = "O prazo de vigencia sera de 12 meses contados da assinatura."
    plain = sparse_vector(passage)
    enriched = sparse_vector(f"{document_terms('x/crk_2025.pdf', [])} {passage}")

    crk_only = sparse_vector("crk")
    assert crk_only[0][0] not in plain[0]
    assert crk_only[0][0] in enriched[0]


def test_an_accented_file_name_yields_whole_folded_terms():
    """The split on [^A-Za-z0-9] severed "Reemissão" at its own accent before
    tokens() could fold it — the second half of the accent bug."""
    terms = document_terms("financeiro/Reemissão_de_boleto.pdf", ["financeiro"])

    assert "reemissao" in terms.split()
    assert "reemiss" not in terms.split()
