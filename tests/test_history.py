"""Conversation history through the API.

The store is swapped for an in-memory one via app.dependency_overrides, the same
seam api/auth.py exists to preserve — so these run in CI with no database. The
SQL itself is covered in test_history_store.py against a real MySQL.

Tokens are minted locally with an RSA keypair, as in test_auth.py: history is
owned by the token's `oid`, so every test here runs with auth on.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docquery.api import auth
from docquery.api.app import app
from docquery.config import Settings, get_settings

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

ANA = "8f3a1c2e-0000-4000-8000-00000000ana1"
BRUNO = "8f3a1c2e-0000-4000-8000-000000bruno"


class InMemoryStore:
    """Same surface as ConversationStore, without the database.

    Ownership is checked the same way — every method takes the owner and a
    mismatch is indistinguishable from a missing conversation.
    """

    def __init__(self) -> None:
        self.conversations: dict[str, str] = {}
        self.rows: dict[str, list[dict]] = {}
        self._next = 0

    def init_schema(self) -> None:
        pass

    def create(self, owner: str) -> str:
        self._next += 1
        cid = f"c-{self._next:04d}"
        self.conversations[cid] = owner
        self.rows[cid] = []
        return cid

    def append(self, conversation_id, owner, question, answer, **fields) -> bool:
        if self.conversations.get(conversation_id) != owner:
            return False
        turns = self.rows[conversation_id]
        turns.append(
            {
                "seq": len(turns) + 1,
                "question": question,
                "answer": answer,
                "rewritten_question": fields.get("rewritten_question", ""),
                "citations": fields.get("citations") or [],
                "model": fields.get("model", ""),
                "tokens_in": fields.get("tokens_in", 0),
                "tokens_out": fields.get("tokens_out", 0),
                "cost_usd": fields.get("cost_usd", 0.0),
                "created_at": datetime.now(UTC),
            }
        )
        return True

    def list_conversations(self, owner, limit=100):
        rows = [
            {
                "id": cid,
                "title": self.rows[cid][0]["question"] if self.rows[cid] else "",
                "created_at": datetime.now(UTC),
                "last_turn_at": None,
                "_order": int(cid.split("-")[1]),
            }
            for cid, o in self.conversations.items()
            if o == owner
        ]
        rows.sort(key=lambda r: r.pop("_order"), reverse=True)
        return rows[:limit]

    def turns(self, conversation_id, owner):
        if self.conversations.get(conversation_id) != owner:
            return None
        return list(self.rows[conversation_id])

    def previous_questions(self, conversation_id, owner, limit):
        if self.conversations.get(conversation_id) != owner:
            return []
        return [t["question"] for t in self.rows[conversation_id]][-limit:]

    def delete(self, conversation_id, owner) -> bool:
        if self.conversations.get(conversation_id) != owner:
            return False
        del self.conversations[conversation_id]
        del self.rows[conversation_id]
        return True


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_key(monkeypatch, private_key):
    public_key = private_key.public_key()
    monkeypatch.setattr(auth, "_get_signing_key", lambda token, settings: public_key)
    return public_key


def token_for(private_key, oid: str, roles=None) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "aud": CLIENT,
            "iss": ISSUER,
            "iat": now,
            "exp": now + timedelta(seconds=3600),
            "sub": oid,
            "oid": oid,
            "roles": roles if roles is not None else ["sector.rh"],
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-kid"},
    )


def _settings(**overrides) -> Settings:
    fields = {
        "auth_enabled": True,
        "azure_tenant_id": TENANT,
        "azure_client_id": CLIENT,
        "history_enabled": True,
        "history_dsn": "mysql://unused:unused@localhost/unused",
        "openai_api_key": "sk-test",
    }
    return Settings(**{**fields, **overrides})


def _pipeline_result(query: str, settings=None, **kwargs) -> dict:
    return {
        "answer": f"resposta para {query}",
        "sources": [],
        "query": query,
        "model": "gpt-4o-mini",
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": 0.0001,
    }


@pytest.fixture
def client(signing_key):
    """TestClient with auth on, a fake store, and the RAG pipeline stubbed."""
    from docquery.api.routes import get_store

    store = InMemoryStore()
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_store] = lambda: store
    with patch("docquery.api.routes.query_pipeline", side_effect=_pipeline_result):
        yield TestClient(app), store
    app.dependency_overrides.clear()


def test_a_query_opens_a_conversation_and_records_the_turn(client, private_key):
    api, store = client

    response = api.post(
        "/query",
        json={"query": "Qual o prazo do contrato Acme?"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    )

    assert response.status_code == 200
    cid = response.json()["conversation_id"]
    assert (
        store.turns(cid, owner=ANA)[0]["question"] == "Qual o prazo do contrato Acme?"
    )


def test_the_owner_reads_their_conversation(client, private_key):
    api, store = client
    headers = {"Authorization": f"Bearer {token_for(private_key, ANA)}"}
    cid = api.post("/query", json={"query": "primeira"}, headers=headers).json()[
        "conversation_id"
    ]

    response = api.get(f"/conversations/{cid}", headers=headers)

    assert response.status_code == 200
    assert [t["question"] for t in response.json()["turns"]] == ["primeira"]


# --- ownership: 404, never 403 --------------------------------------------


def test_another_owner_gets_404_not_403(client, private_key):
    """403 would confirm the id exists, which is what id-guessing is after.

    Meaningful only because the test above proves the route answers 200 for the
    owner — otherwise a missing route would produce this same 404.
    """
    api, store = client
    cid = api.post(
        "/query",
        json={"query": "folha de pagamento"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    ).json()["conversation_id"]

    response = api.get(
        f"/conversations/{cid}",
        headers={"Authorization": f"Bearer {token_for(private_key, BRUNO)}"},
    )

    assert response.status_code == 404


def test_another_owner_cannot_continue_the_conversation(client, private_key):
    """Nor use it as an oracle: continuing must not confirm the id either."""
    api, store = client
    cid = api.post(
        "/query",
        json={"query": "folha de pagamento"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    ).json()["conversation_id"]

    response = api.post(
        "/query",
        json={"query": "e o valor?", "conversation_id": cid},
        headers={"Authorization": f"Bearer {token_for(private_key, BRUNO)}"},
    )

    assert response.status_code == 404
    assert len(store.turns(cid, owner=ANA)) == 1


def test_another_owner_cannot_delete(client, private_key):
    api, store = client
    cid = api.post(
        "/query",
        json={"query": "x"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    ).json()["conversation_id"]

    response = api.delete(
        f"/conversations/{cid}",
        headers={"Authorization": f"Bearer {token_for(private_key, BRUNO)}"},
    )

    assert response.status_code == 404
    assert store.turns(cid, owner=ANA) is not None


def test_the_owner_can_erase_their_conversation(client, private_key):
    api, store = client
    headers = {"Authorization": f"Bearer {token_for(private_key, ANA)}"}
    cid = api.post("/query", json={"query": "x"}, headers=headers).json()[
        "conversation_id"
    ]

    assert api.delete(f"/conversations/{cid}", headers=headers).status_code == 204
    assert api.get(f"/conversations/{cid}", headers=headers).status_code == 404


# --- the follow-up actually being resolved ---------------------------------


def test_a_follow_up_is_rewritten_before_retrieval(client, private_key, monkeypatch):
    """The whole point: "e a multa?" must reach retrieval with its anchor."""
    from docquery.api import routes

    monkeypatch.setattr(
        routes,
        "contextualize",
        lambda query, previous, settings, openai_client: (
            "multa por atraso no contrato Acme"
        ),
    )
    monkeypatch.setattr(routes, "OpenAI", lambda **kwargs: None)

    api, store = client
    headers = {"Authorization": f"Bearer {token_for(private_key, ANA)}"}
    cid = api.post(
        "/query",
        json={"query": "Qual o prazo do contrato Acme?"},
        headers=headers,
    ).json()["conversation_id"]

    response = api.post(
        "/query",
        json={"query": "e a multa?", "conversation_id": cid},
        headers=headers,
    ).json()

    assert response["rewritten_query"] == "multa por atraso no contrato Acme"
    # The answer came from retrieving the resolved question...
    assert "multa por atraso no contrato Acme" in response["answer"]
    # ...but the caller asked their own question, and that is what is echoed
    # back and what the history records.
    assert response["query"] == "e a multa?"
    assert store.turns(cid, owner=ANA)[1]["question"] == "e a multa?"


def test_a_first_turn_reports_no_rewrite(client, private_key):
    api, _ = client
    response = api.post(
        "/query",
        json={"query": "Qual o prazo?"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    ).json()

    assert response["rewritten_query"] is None


def test_the_list_shows_only_your_own_conversations(client, private_key):
    api, _ = client
    ana = {"Authorization": f"Bearer {token_for(private_key, ANA)}"}
    bruno = {"Authorization": f"Bearer {token_for(private_key, BRUNO)}"}
    api.post("/query", json={"query": "pergunta da ana"}, headers=ana)
    api.post("/query", json={"query": "pergunta do bruno"}, headers=bruno)

    listing = api.get("/conversations", headers=ana).json()["conversations"]

    assert [c["title"] for c in listing] == ["pergunta da ana"]


def test_the_list_is_most_recent_first(client, private_key):
    api, _ = client
    headers = {"Authorization": f"Bearer {token_for(private_key, ANA)}"}
    for q in ["primeira", "segunda", "terceira"]:
        api.post("/query", json={"query": q}, headers=headers)

    listing = api.get("/conversations", headers=headers).json()["conversations"]

    assert [c["title"] for c in listing] == ["terceira", "segunda", "primeira"]


# --- streaming -------------------------------------------------------------


def _sse_events(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs."""
    events = []
    for block in body.strip().split("\n\n"):
        event = data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event:
            events.append((event, data))
    return events


@pytest.fixture
def streaming_client(signing_key, monkeypatch):
    from docquery.api import routes
    from docquery.api.routes import get_store

    def _stream(query, settings=None, **kwargs):
        yield {
            "type": "sources",
            "sources": [
                {
                    "index": 1,
                    "source": "data/contracts/crk_2025.pdf",
                    "chunk_index": 4,
                    "score": 7.1,
                    "text": "O prazo de vigencia e de 12 meses.",
                    "section": "1.10 VIGENCIA",
                    "folders": ["contracts"],
                }
            ],
        }
        yield {"type": "token", "text": "O prazo "}
        yield {"type": "token", "text": "e de 30 dias."}
        yield {
            "type": "done",
            "answer": "O prazo e de 30 dias.",
            "query": query,
            "model": "gpt-4o-mini",
            "tokens_in": 10,
            "tokens_out": 5,
            "cost_usd": 0.0001,
        }

    monkeypatch.setattr(routes, "query_pipeline_stream", _stream)
    store = InMemoryStore()
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app), store
    app.dependency_overrides.clear()


def test_the_stream_sends_sources_before_any_token(streaming_client, private_key):
    api, _ = streaming_client

    response = api.post(
        "/query/stream",
        json={"query": "Qual o prazo?"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    kinds = [name for name, _ in _sse_events(response.text)]
    assert kinds[0] == "sources"
    assert kinds[-1] == "done"


def test_the_stream_records_the_turn_when_it_finishes(streaming_client, private_key):
    api, store = streaming_client

    response = api.post(
        "/query/stream",
        json={"query": "Qual o prazo?"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    )

    done = [data for name, data in _sse_events(response.text) if name == "done"][0]
    cid = json.loads(done)["conversation_id"]
    turn = store.turns(cid, owner=ANA)[0]
    assert turn["question"] == "Qual o prazo?"
    assert turn["answer"] == "O prazo e de 30 dias."


def test_the_stream_records_the_citations_it_sent(streaming_client, private_key):
    """Reopening a conversation must show the same sources the answer did.

    The sources are emitted in their own event before the first token — that is
    the point of the streaming design — so the turn has to be recorded from what
    was sent, not from the closing event, which carries no sources at all.
    Without this, every streamed turn was stored with an empty citation list and
    history rendered answers whose [1] markers pointed at nothing.
    """
    api, store = streaming_client

    response = api.post(
        "/query/stream",
        json={"query": "Qual o prazo?"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    )

    sent = [
        json.loads(data)
        for name, data in _sse_events(response.text)
        if name == "sources"
    ][0]["sources"]
    cid = json.loads([d for n, d in _sse_events(response.text) if n == "done"][0])[
        "conversation_id"
    ]

    stored = store.turns(cid, owner=ANA)[0]["citations"]
    assert stored == sent
    assert stored, "the fixture streams at least one source"


def test_the_stream_asks_the_proxy_not_to_buffer(streaming_client, private_key):
    """Without this a reverse proxy delivers the whole body at once.

    The stream still 'works' in that case — it just stops being a stream, with
    no error anywhere to say so, which is the worst way for it to fail.
    """
    api, _ = streaming_client

    response = api.post(
        "/query/stream",
        json={"query": "x"},
        headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
    )

    assert response.headers["x-accel-buffering"] == "no"
    # no-store comes from SecurityHeadersMiddleware and is stricter than the
    # no-cache an SSE endpoint would ask for, so the endpoint does not set one.
    assert response.headers["cache-control"] == "no-store"


def test_a_question_with_a_cpf_is_stored_redacted(signing_key, private_key):
    """Redaction happens before any persistence — history included.

    The question and the answer are the two strings that reach MySQL without
    passing through the ingest pipeline, so they carry their own seam.
    """
    from docquery.api.routes import get_store

    store = InMemoryStore()
    app.dependency_overrides[get_settings] = lambda: _settings(
        pii_redaction_enabled=True
    )
    app.dependency_overrides[get_store] = lambda: store
    try:
        with patch("docquery.api.routes.query_pipeline", side_effect=_pipeline_result):
            api = TestClient(app)
            response = api.post(
                "/query",
                json={"query": "o CPF 529.982.247-25 tem contrato ativo?"},
                headers={"Authorization": f"Bearer {token_for(private_key, ANA)}"},
            )

        assert response.status_code == 200
        turn = store.turns(response.json()["conversation_id"], owner=ANA)[0]
        assert turn["question"] == "o CPF [CPF] tem contrato ativo?"
        assert "529.982.247-25" not in turn["answer"]
    finally:
        app.dependency_overrides.clear()
