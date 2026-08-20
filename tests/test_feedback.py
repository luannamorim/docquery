"""Outdated-document reports through the API.

The store is swapped for an in-memory one via app.dependency_overrides and the
Qdrant sector lookup is monkeypatched, so these run in CI with neither a
database nor an index. The SQL is covered in test_feedback_store.py against a
real MySQL.

Tokens are minted locally with an RSA keypair, as in test_history.py: a report
is deduplicated by the token's oid and read back by sector, so every test here
runs with auth on.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docquery.api import auth, routes
from docquery.api.app import app
from docquery.config import Settings, get_settings
from docquery.feedback.store import source_hash

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

ANA = "8f3a1c2e-0000-4000-8000-00000000ana1"
BRUNO = "8f3a1c2e-0000-4000-8000-000000bruno"
CLARA = "8f3a1c2e-0000-4000-8000-0000000clara"

CONTRATO = "data/financeiro/contrato_acme.pdf"
FERIAS = "data/rh/ferias.md"

# What the (stubbed) index knows: source → sector.
CORPUS = {CONTRATO: "financeiro", FERIAS: "rh"}


class InMemoryFeedbackStore:
    """Same surface as FeedbackStore, without the database.

    The sector predicate is honoured the same way — every read and the resolve
    take the caller's sectors, and a mismatch is indistinguishable from a
    document nobody reported.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self._clock = 0

    def init_schema(self) -> None:
        pass

    def report(self, source, sector, reporter, comment="") -> bool:
        key = (source_hash(source), reporter)
        created = key not in self.rows
        self._clock += 1
        self.rows[key] = {
            "source": source,
            "sector": sector,
            "comment": comment,
            "at": self._clock,
        }
        return created

    def list_reports(self, sectors, limit=200):
        sectors = None if sectors is None else [s for s in sectors if s]
        if sectors is not None and not sectors:
            return []
        groups: dict[tuple[str, str], list[dict]] = {}
        for (h, _reporter), row in self.rows.items():
            if sectors is None or row["sector"] in sectors:
                groups.setdefault((h, row["sector"]), []).append(row)
        docs = [
            {
                "source": rows[0]["source"],
                "sector": sector,
                "report_count": len(rows),
                "last_reported_at": datetime.now(UTC),
                "comments": [r["comment"] for r in rows if r["comment"]],
                "_at": max(r["at"] for r in rows),
            }
            for (h, sector), rows in groups.items()
        ]
        docs.sort(key=lambda d: d.pop("_at"), reverse=True)
        return docs[:limit]

    def reported(self, sources, sectors) -> set:
        sectors = None if sectors is None else [s for s in sectors if s]
        if not sources or (sectors is not None and not sectors):
            return set()
        hashes = {source_hash(s): s for s in sources}
        return {
            hashes[key[0]]
            for key, row in self.rows.items()
            if key[0] in hashes and (sectors is None or row["sector"] in sectors)
        }

    def resolve(self, source, sectors) -> bool:
        sectors = None if sectors is None else [s for s in sectors if s]
        if sectors is not None and not sectors:
            return False
        h = source_hash(source)
        matched = [
            key
            for key, row in self.rows.items()
            if key[0] == h and (sectors is None or row["sector"] in sectors)
        ]
        for key in matched:
            del self.rows[key]
        return bool(matched)


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
            "roles": roles if roles is not None else ["sector.financeiro"],
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
        "feedback_enabled": True,
        "history_dsn": "mysql://unused:unused@localhost/unused",
        "openai_api_key": "sk-test",
    }
    return Settings(**{**fields, **overrides})


def _fake_sector_for_source(source, settings, sectors):
    """The lookup's contract, over the canned corpus: the sector filter is
    part of the query, so a source outside the caller's sectors is None."""
    if sectors is not None:
        sectors = [s for s in sectors if s]
        if not sectors:
            return None
    sector = CORPUS.get(source)
    if sector is None:
        return None
    if sectors is not None and sector not in sectors:
        return None
    return sector


@pytest.fixture
def client(signing_key, monkeypatch):
    """TestClient with auth on, a fake store, and the sector lookup stubbed."""
    from docquery.api.routes import get_feedback_store

    store = InMemoryFeedbackStore()
    app.dependency_overrides[get_settings] = lambda: _settings()
    app.dependency_overrides[get_feedback_store] = lambda: store
    monkeypatch.setattr(routes, "sector_for_source", _fake_sector_for_source)
    yield TestClient(app), store
    app.dependency_overrides.clear()


def _auth(private_key, oid=ANA, roles=None):
    return {"Authorization": f"Bearer {token_for(private_key, oid, roles)}"}


def test_a_report_is_recorded_and_listed(client, private_key):
    api, _ = client

    response = api.post(
        "/feedback",
        json={"source": CONTRATO, "comment": "valores de 2023"},
        headers=_auth(private_key),
    )

    assert response.status_code == 201
    assert response.json() == {
        "source": CONTRATO,
        "sector": "financeiro",
        "created": True,
    }

    listed = api.get("/feedback", headers=_auth(private_key)).json()
    assert len(listed["documents"]) == 1
    doc = listed["documents"][0]
    assert doc["source"] == CONTRATO
    assert doc["report_count"] == 1
    assert doc["comments"] == ["valores de 2023"]


def test_a_repeat_report_updates_instead_of_duplicating(client, private_key):
    api, _ = client
    api.post("/feedback", json={"source": CONTRATO}, headers=_auth(private_key))

    response = api.post(
        "/feedback",
        json={"source": CONTRATO, "comment": "versão nova no drive"},
        headers=_auth(private_key),
    )

    assert response.status_code == 200
    assert response.json()["created"] is False
    doc = api.get("/feedback", headers=_auth(private_key)).json()["documents"][0]
    assert doc["report_count"] == 1
    assert doc["comments"] == ["versão nova no drive"]


def test_reports_aggregate_across_reporters(client, private_key):
    api, _ = client
    api.post("/feedback", json={"source": CONTRATO}, headers=_auth(private_key, ANA))
    api.post("/feedback", json={"source": CONTRATO}, headers=_auth(private_key, BRUNO))

    doc = api.get("/feedback", headers=_auth(private_key, CLARA)).json()["documents"][0]

    assert doc["report_count"] == 2


def test_a_document_outside_the_callers_sectors_is_not_found(client, private_key):
    """404 and never 403 — flagging must not confirm the document exists."""
    api, _ = client

    response = api.post(
        "/feedback",
        json={"source": FERIAS},
        headers=_auth(private_key, roles=["sector.financeiro"]),
    )

    assert response.status_code == 404


def test_an_unknown_source_is_not_found(client, private_key):
    api, _ = client

    response = api.post(
        "/feedback", json={"source": "data/nada.pdf"}, headers=_auth(private_key)
    )

    assert response.status_code == 404


def test_a_token_with_no_sectors_reads_and_writes_nothing(client, private_key):
    api, store = client
    store.report(CONTRATO, "financeiro", BRUNO)

    headers = _auth(private_key, roles=[])
    assert api.post("/feedback", json={"source": CONTRATO}, headers=headers).status_code == 404
    assert api.get("/feedback", headers=headers).json() == {"documents": []}


def test_the_list_is_scoped_to_the_callers_sectors(client, private_key):
    api, store = client
    store.report(CONTRATO, "financeiro", ANA)
    store.report(FERIAS, "rh", ANA)

    listed = api.get(
        "/feedback", headers=_auth(private_key, roles=["sector.rh"])
    ).json()

    assert [d["source"] for d in listed["documents"]] == [FERIAS]


def test_any_member_of_the_sector_can_resolve(client, private_key):
    api, _ = client
    api.post("/feedback", json={"source": CONTRATO}, headers=_auth(private_key, ANA))

    response = api.post(
        "/feedback/resolve",
        json={"source": CONTRATO},
        headers=_auth(private_key, BRUNO),
    )

    assert response.status_code == 204
    assert api.get("/feedback", headers=_auth(private_key)).json() == {"documents": []}


def test_resolving_from_outside_the_sector_is_not_found(client, private_key):
    api, store = client
    store.report(CONTRATO, "financeiro", ANA)

    response = api.post(
        "/feedback",
        json={"source": CONTRATO},
        headers=_auth(private_key, roles=["sector.rh"]),
    )
    assert response.status_code == 404

    resolve = api.post(
        "/feedback/resolve",
        json={"source": CONTRATO},
        headers=_auth(private_key, roles=["sector.rh"]),
    )
    assert resolve.status_code == 404
    assert len(store.list_reports(sectors=None)) == 1


def test_feedback_off_answers_like_the_feature_never_existed(
    signing_key, private_key, monkeypatch
):
    from docquery.api.routes import get_feedback_store

    app.dependency_overrides[get_settings] = lambda: _settings(feedback_enabled=False)
    app.dependency_overrides[get_feedback_store] = lambda: None
    monkeypatch.setattr(routes, "sector_for_source", _fake_sector_for_source)
    try:
        api = TestClient(app)
        headers = _auth(private_key)
        assert api.get("/feedback", headers=headers).json() == {"documents": []}
        assert (
            api.post("/feedback", json={"source": CONTRATO}, headers=headers)
        ).status_code == 404
        assert (
            api.post("/feedback/resolve", json={"source": CONTRATO}, headers=headers)
        ).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_feedback_requires_a_token(client):
    api, _ = client

    assert api.post("/feedback", json={"source": CONTRATO}).status_code == 401
    assert api.get("/feedback").status_code == 401


def test_config_tells_the_browser_whether_feedback_is_on(client):
    api, _ = client

    assert api.get("/config").json()["feedbackEnabled"] is True


# --- The flag on query sources ----------------------------------------------
#
# A report used to be invisible outside the review list; now every asker sees
# it as a `flagged` bit on the sources of a query answer — existence only,
# comments stay in /feedback. These drive the two query endpoints with the
# pipeline patched, the same way test_api.py does.

TABELA = "data/financeiro/tabela_precos.md"


def _pipeline_result(*sources):
    return {
        "answer": "ok [1]",
        "sources": [
            {"index": i + 1, "source": s, "chunk_index": 0, "score": 1.0, "text": "trecho"}
            for i, s in enumerate(sources)
        ],
        "query": "q",
        "model": "gpt-4o-mini",
    }


def test_query_sources_carry_the_flag_of_an_open_report(client, private_key):
    api, store = client
    store.report(CONTRATO, "financeiro", BRUNO, "valores de 2023")

    with patch(
        "docquery.api.routes.query_pipeline",
        return_value=_pipeline_result(CONTRATO, TABELA),
    ):
        response = api.post("/query", json={"query": "q"}, headers=_auth(private_key))

    assert response.status_code == 200
    flags = {s["source"]: s["flagged"] for s in response.json()["sources"]}
    assert flags == {CONTRATO: True, TABELA: False}


def test_the_flag_honours_the_callers_sectors(client, private_key):
    """A report in a sector the caller cannot read is indistinguishable from
    no report — the flag must not leak across compartments."""
    api, store = client
    store.report(CONTRATO, "financeiro", BRUNO)

    with patch(
        "docquery.api.routes.query_pipeline",
        return_value=_pipeline_result(CONTRATO),
    ):
        response = api.post(
            "/query",
            json={"query": "q"},
            headers=_auth(private_key, roles=["sector.rh"]),
        )

    assert response.json()["sources"][0]["flagged"] is False


def test_feedback_off_leaves_sources_unflagged(signing_key, private_key, monkeypatch):
    from docquery.api.routes import get_feedback_store

    app.dependency_overrides[get_settings] = lambda: _settings(feedback_enabled=False)
    app.dependency_overrides[get_feedback_store] = lambda: None
    try:
        api = TestClient(app)
        with patch(
            "docquery.api.routes.query_pipeline",
            return_value=_pipeline_result(CONTRATO),
        ):
            response = api.post(
                "/query", json={"query": "q"}, headers=_auth(private_key)
            )
        assert response.status_code == 200
        assert response.json()["sources"][0]["flagged"] is False
    finally:
        app.dependency_overrides.clear()


def test_a_failing_feedback_lookup_does_not_break_the_query(client, private_key):
    """Answering questions must not depend on the feedback database: a lookup
    failure degrades to unflagged sources, never to a 500."""
    api, _ = client

    class ExplodingStore(InMemoryFeedbackStore):
        def reported(self, sources, sectors):
            raise RuntimeError("mysql is down")

    from docquery.api.routes import get_feedback_store

    app.dependency_overrides[get_feedback_store] = lambda: ExplodingStore()
    with patch(
        "docquery.api.routes.query_pipeline",
        return_value=_pipeline_result(CONTRATO),
    ):
        response = api.post("/query", json={"query": "q"}, headers=_auth(private_key))

    assert response.status_code == 200
    assert response.json()["sources"][0]["flagged"] is False


def test_the_stream_sources_frame_carries_the_flag(client, private_key, monkeypatch):
    api, store = client
    store.report(CONTRATO, "financeiro", BRUNO)

    def _stream(query, settings=None, **kwargs):
        yield {"type": "sources", "sources": _pipeline_result(CONTRATO)["sources"]}
        yield {"type": "token", "text": "ok"}
        yield {
            "type": "done",
            "answer": "ok",
            "query": query,
            "model": "gpt-4o-mini",
            "tokens_in": 1,
            "tokens_out": 1,
            "cost_usd": 0.0,
        }

    monkeypatch.setattr(routes, "query_pipeline_stream", _stream)
    response = api.post("/query/stream", json={"query": "q"}, headers=_auth(private_key))

    assert response.status_code == 200
    frames = dict(
        (event, data)
        for block in response.text.strip().split("\n\n")
        for event, data in [
            (
                next(
                    (line[len("event: ") :] for line in block.splitlines()
                     if line.startswith("event: ")),
                    "",
                ),
                next(
                    (line[len("data: ") :] for line in block.splitlines()
                     if line.startswith("data: ")),
                    "",
                ),
            )
        ]
        if event
    )
    sources = json.loads(frames["sources"])["sources"]
    assert sources[0]["flagged"] is True
