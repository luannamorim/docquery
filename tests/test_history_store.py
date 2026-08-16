"""The conversation store, against a real MySQL.

Opt-in, like the Docling conversion tests: CI runs the fast tests that drive the
endpoints through a fake store, while these exercise the SQL that actually
ships. Ownership is the property worth a real database — it is enforced in the
WHERE clause of every statement, not in Python above them, so a test against a
stub would prove nothing about it.

Enable with a throwaway server, e.g.:
    docker run -d --name docquery-mysql-test -e MYSQL_ROOT_PASSWORD=test \\
        -e MYSQL_DATABASE=docquery_test -p 13306:3306 mysql:8
    DOCQUERY_MYSQL_TEST_DSN='mysql://root:test@127.0.0.1:13306/docquery_test' \\
        uv run pytest -m mysql
"""

import os

import pytest

from docquery.history.store import ConversationStore

DSN = os.environ.get("DOCQUERY_MYSQL_TEST_DSN", "")

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(
        not DSN, reason="set DOCQUERY_MYSQL_TEST_DSN to run the store tests"
    ),
]

ANA = "8f3a1c2e-0000-4000-8000-00000000ana1"
BRUNO = "8f3a1c2e-0000-4000-8000-000000bruno"


@pytest.fixture
def store():
    s = ConversationStore(DSN)
    s.init_schema()
    s.reset_for_tests()
    return s


def test_a_conversation_belongs_to_the_token_that_opened_it(store):
    cid = store.create(owner=ANA)
    store.append(
        cid, owner=ANA, question="Qual o prazo do contrato Acme?", answer="30 dias"
    )

    assert (
        store.turns(cid, owner=ANA)[0]["question"] == "Qual o prazo do contrato Acme?"
    )


def test_another_owner_sees_nothing_at_all(store):
    """Not an empty conversation — no conversation.

    The endpoint turns this into a 404. A 403 would confirm the id exists, which
    is exactly what someone guessing ids is trying to learn.
    """
    cid = store.create(owner=ANA)
    store.append(cid, owner=ANA, question="folha de pagamento", answer="…")

    assert store.turns(cid, owner=BRUNO) is None


def test_another_owner_cannot_write_into_the_conversation(store):
    cid = store.create(owner=ANA)

    assert store.append(cid, owner=BRUNO, question="x", answer="y") is False
    assert store.turns(cid, owner=ANA) == []


def test_an_unknown_id_is_not_an_error(store):
    """A guessed id behaves exactly like someone else's: nothing there."""
    assert store.turns("00000000-0000-4000-8000-000000000000", owner=ANA) is None
    assert store.append("00000000-0000-4000-8000-000000000000", ANA, "q", "a") is False


def test_turns_are_numbered_in_order(store):
    cid = store.create(owner=ANA)
    for i in range(3):
        store.append(cid, owner=ANA, question=f"pergunta {i}", answer=f"resposta {i}")

    assert [t["seq"] for t in store.turns(cid, owner=ANA)] == [1, 2, 3]


def test_previous_questions_are_oldest_first_and_capped(store):
    cid = store.create(owner=ANA)
    for i in range(5):
        store.append(cid, owner=ANA, question=f"p{i}", answer=f"r{i}")

    assert store.previous_questions(cid, owner=ANA, limit=3) == ["p2", "p3", "p4"]


def test_previous_questions_carry_no_answer_text(store):
    """The rewriter's only input. An answer reaching it would carry document
    passages back into a prompt — see contextualize.py."""
    cid = store.create(owner=ANA)
    store.append(cid, owner=ANA, question="p", answer="SEGREDO DO DOCUMENTO")

    assert store.previous_questions(cid, owner=ANA, limit=10) == ["p"]


def test_previous_questions_of_another_owner_are_empty(store):
    cid = store.create(owner=ANA)
    store.append(cid, owner=ANA, question="p", answer="r")

    assert store.previous_questions(cid, owner=BRUNO, limit=10) == []


def test_deleting_erases_the_turns_too(store):
    cid = store.create(owner=ANA)
    store.append(cid, owner=ANA, question="p", answer="r")

    assert store.delete(cid, owner=ANA) is True
    assert store.turns(cid, owner=ANA) is None


def test_another_owner_cannot_delete(store):
    cid = store.create(owner=ANA)

    assert store.delete(cid, owner=BRUNO) is False
    assert store.turns(cid, owner=ANA) == []


def test_citations_survive_the_round_trip(store):
    cid = store.create(owner=ANA)
    citations = [{"index": 1, "source": "data/rh/ferias.md", "chunk_index": 0}]
    store.append(cid, owner=ANA, question="p", answer="r", citations=citations)

    assert store.turns(cid, owner=ANA)[0]["citations"] == citations
