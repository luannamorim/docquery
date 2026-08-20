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
from datetime import UTC, datetime, timedelta

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
    assert (
        store.report(CONTRATO, "financeiro", ANA, "valores de 2023", "Ana Lima")
        is True
    )

    docs = store.list_reports(sectors=None)
    assert len(docs) == 1
    assert docs[0]["source"] == CONTRATO
    assert docs[0]["sector"] == "financeiro"
    assert docs[0]["report_count"] == 1
    assert [c["comment"] for c in docs[0]["comments"]] == ["valores de 2023"]
    # Each comment carries when and by whom, so the review panel can say both.
    assert docs[0]["comments"][0]["reported_at"] is not None
    assert docs[0]["comments"][0]["reporter_name"] == "Ana Lima"


def test_a_repeat_report_by_the_same_reporter_updates_instead_of_duplicating(store):
    store.report(CONTRATO, "financeiro", ANA, "valores antigos")

    assert store.report(CONTRATO, "financeiro", ANA, "já existe versão nova") is False

    docs = store.list_reports(sectors=None)
    assert docs[0]["report_count"] == 1
    assert [c["comment"] for c in docs[0]["comments"]] == ["já existe versão nova"]


def test_two_reporters_aggregate_on_the_same_document(store):
    store.report(CONTRATO, "financeiro", ANA, "cláusula 3 mudou")
    store.report(CONTRATO, "financeiro", BRUNO, "")

    docs = store.list_reports(sectors=None)
    assert len(docs) == 1
    assert docs[0]["report_count"] == 2
    # Empty comments carry nothing worth listing.
    assert [c["comment"] for c in docs[0]["comments"]] == ["cláusula 3 mudou"]


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


def test_reported_names_the_sources_with_open_reports(store):
    store.report(CONTRATO, "financeiro", ANA, "valores de 2023")

    assert store.reported([CONTRATO, FERIAS], sectors=None) == {CONTRATO}


def test_reported_is_scoped_by_the_callers_sectors(store):
    """A report whose sector the caller cannot read is indistinguishable from
    no report — the same compartment rule list_reports follows."""
    store.report(CONTRATO, "financeiro", ANA)

    assert store.reported([CONTRATO], sectors=["rh"]) == set()
    assert store.reported([CONTRATO], sectors=["financeiro"]) == {CONTRATO}
    assert store.reported([CONTRATO], sectors=[]) == set()
    assert store.reported([CONTRATO], sectors=[""]) == set()


def test_reported_with_no_sources_never_touches_the_database(store):
    assert store.reported([], sectors=None) == set()


def test_resolving_clears_the_reported_bit(store):
    store.report(CONTRATO, "financeiro", ANA)
    store.resolve(CONTRATO, sectors=["financeiro"])

    assert store.reported([CONTRATO], sectors=None) == set()


def test_timestamps_come_back_timezone_aware_in_utc(store):
    """pymysql hands back naive datetimes in the session time zone. Naive
    survives Pydantic as an offset-less ISO string, which the browser then
    reparses as *its* local time — so the store must pin the session to UTC
    and return aware datetimes for the offset to reach the JSON."""
    before = datetime.now(UTC) - timedelta(seconds=1)
    store.report(CONTRATO, "financeiro", ANA, "valores de 2023")
    after = datetime.now(UTC) + timedelta(seconds=1)

    doc = store.list_reports(sectors=None)[0]
    for stamp in (doc["last_reported_at"], doc["comments"][0]["reported_at"]):
        assert stamp.tzinfo is not None
        assert before <= stamp <= after


def test_a_long_remote_uri_survives_the_round_trip(store):
    """Remote sources are URIs that outgrow any indexable VARCHAR — identity
    is the hash, but the display column must give the full string back."""
    uri = "sharepoint://contoso.sharepoint.com/sites/docs/" + "a" * 800 + ".pdf"
    store.report(uri, "financeiro", ANA)

    assert store.list_reports(sectors=None)[0]["source"] == uri
