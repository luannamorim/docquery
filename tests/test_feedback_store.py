"""The feedback store, against a real MySQL.

Opt-in like the conversation store tests: CI drives the endpoints through a
fake store, while these exercise the SQL that ships. The properties worth a
real database here are the UNIQUE key (one report per reporter per document,
updates instead of duplicating) and the sector predicate — both live in the
statements, not in Python above them.

Enable with the same throwaway server as test_history_store.py:
    docker run -d --name docquery-mysql-test -e MYSQL_ROOT_PASSWORD=test \\
        -e MYSQL_DATABASE=docquery_test -p 13306:3306 mysql:8
    DOCQUERY_MYSQL_TEST_DSN='mysql://root:test@127.0.0.1:13306/docquery_test' \\
        uv run pytest -m mysql
"""

import os

import pytest

from docquery.feedback.store import FeedbackStore

DSN = os.environ.get("DOCQUERY_MYSQL_TEST_DSN", "")

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(
        not DSN, reason="set DOCQUERY_MYSQL_TEST_DSN to run the store tests"
    ),
]

ANA = "8f3a1c2e-0000-4000-8000-00000000ana1"
BRUNO = "8f3a1c2e-0000-4000-8000-000000bruno"

CONTRATO = "data/financeiro/contrato_acme.pdf"
FERIAS = "data/rh/ferias.md"


@pytest.fixture
def store():
    s = FeedbackStore(DSN)
    s.init_schema()
    s.reset_for_tests()
    return s


def test_a_first_report_is_created(store):
    assert store.report(CONTRATO, "financeiro", ANA, "valores de 2023") is True

    docs = store.list_reports(sectors=None)
    assert len(docs) == 1
    assert docs[0]["source"] == CONTRATO
    assert docs[0]["sector"] == "financeiro"
    assert docs[0]["report_count"] == 1
    assert docs[0]["comments"] == ["valores de 2023"]


def test_a_repeat_report_by_the_same_reporter_updates_instead_of_duplicating(store):
    store.report(CONTRATO, "financeiro", ANA, "valores antigos")

    assert store.report(CONTRATO, "financeiro", ANA, "já existe versão nova") is False

    docs = store.list_reports(sectors=None)
    assert docs[0]["report_count"] == 1
    assert docs[0]["comments"] == ["já existe versão nova"]


def test_two_reporters_aggregate_on_the_same_document(store):
    store.report(CONTRATO, "financeiro", ANA, "cláusula 3 mudou")
    store.report(CONTRATO, "financeiro", BRUNO, "")

    docs = store.list_reports(sectors=None)
    assert len(docs) == 1
    assert docs[0]["report_count"] == 2
    # Empty comments carry nothing worth listing.
    assert docs[0]["comments"] == ["cláusula 3 mudou"]


def test_the_list_is_scoped_by_sector(store):
    store.report(CONTRATO, "financeiro", ANA)
    store.report(FERIAS, "rh", ANA)

    docs = store.list_reports(sectors=["rh"])
    assert [d["source"] for d in docs] == [FERIAS]


def test_no_sectors_reads_nothing_and_none_reads_everything(store):
    store.report(CONTRATO, "financeiro", ANA)

    assert store.list_reports(sectors=[]) == []
    assert len(store.list_reports(sectors=None)) == 1


def test_blank_sectors_are_dropped_not_matched(store):
    """A chunk whose sector is "" is unreachable by any role; a blank in the
    caller's list must not become a predicate that matches it."""
    store.report(CONTRATO, "financeiro", ANA)

    assert store.list_reports(sectors=[""]) == []
    assert [d["source"] for d in store.list_reports(sectors=["", "financeiro"])] == [
        CONTRATO
    ]


def test_resolving_erases_every_report_within_the_sector(store):
    store.report(CONTRATO, "financeiro", ANA)
    store.report(CONTRATO, "financeiro", BRUNO)

    assert store.resolve(CONTRATO, sectors=["financeiro"]) is True
    assert store.list_reports(sectors=None) == []


def test_resolving_from_outside_the_sector_touches_nothing(store):
    store.report(CONTRATO, "financeiro", ANA)

    assert store.resolve(CONTRATO, sectors=["rh"]) is False
    assert store.resolve(CONTRATO, sectors=[]) is False
    assert len(store.list_reports(sectors=None)) == 1


def test_a_long_remote_uri_survives_the_round_trip(store):
    """Remote sources are URIs that outgrow any indexable VARCHAR — identity
    is the hash, but the display column must give the full string back."""
    uri = "sharepoint://contoso.sharepoint.com/sites/docs/" + "a" * 800 + ".pdf"
    store.report(uri, "financeiro", ANA)

    assert store.list_reports(sectors=None)[0]["source"] == uri
