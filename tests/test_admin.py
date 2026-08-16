"""Ingestion is an operator action, not something any employee may trigger.

`POST /ingest` required a valid token and nothing more, so anyone in the tenant
could start a re-index. The path allowlists bound what it can read, so this was
never a way to reach data — but a re-ingest deletes a source's chunks before
writing the new ones, and with OCR that window is minutes long. One ordinary
user could empty the corpus for everybody, repeatedly, and nothing in the
system would say who did it.

403 here, not the 404 the conversation routes answer with: /ingest is in the
OpenAPI document and its existence is not a secret. Hiding it would only
confuse the operator who does hold the role.
"""

from datetime import UTC, datetime, timedelta

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


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_key(monkeypatch, private_key):
    public_key = private_key.public_key()
    monkeypatch.setattr(auth, "_get_signing_key", lambda token, settings: public_key)
    return public_key


def _token(private_key, roles: list[str]) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "aud": CLIENT,
            "iss": ISSUER,
            "iat": now,
            "exp": now + timedelta(seconds=3600),
            "sub": "user-1",
            "oid": "8f3a1c2e-0000-4000-8000-00000000ana1",
            "roles": roles,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-kid"},
    )


def _client(**overrides) -> TestClient:
    fields = {
        "auth_enabled": True,
        "azure_tenant_id": TENANT,
        "azure_client_id": CLIENT,
        "openai_api_key": "sk-test",
    }
    settings = Settings(**{**fields, **overrides})
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _post_ingest(client: TestClient, token: str):
    return client.post(
        "/ingest",
        json={"path": "docs/definitely-not-here"},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_sector_role_alone_cannot_start_an_ingest(private_key, signing_key):
    client = _client()
    try:
        response = _post_ingest(client, _token(private_key, ["sector.contracts"]))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_a_token_with_no_roles_cannot_either(private_key, signing_key):
    client = _client()
    try:
        response = _post_ingest(client, _token(private_key, []))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_the_admin_role_gets_through(private_key, signing_key):
    """404 because the path does not exist — which is the handler answering.

    Reaching the handler at all is the assertion: the gate let it past.
    """
    client = _client()
    try:
        response = _post_ingest(client, _token(private_key, ["docquery.admin"]))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_the_status_route_is_gated_the_same_way(private_key, signing_key):
    """It reports on ingests, so it leaks the same operational picture."""
    client = _client()
    try:
        response = client.get(
            "/ingest/some-task-id",
            headers={"Authorization": f"Bearer {_token(private_key, ['sector.rh'])}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_querying_is_untouched_by_the_admin_role(private_key, signing_key):
    """An ordinary reader must not need an operator role to ask a question."""
    from unittest.mock import patch

    client = _client()
    result = {
        "answer": "ok",
        "sources": [],
        "query": "x",
        "model": "gpt-4o-mini",
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }
    try:
        with patch("docquery.api.routes.query_pipeline", return_value=result):
            response = client.post(
                "/query",
                json={"query": "x"},
                headers={
                    "Authorization": f"Bearer {_token(private_key, ['sector.rh'])}"
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_the_role_name_is_configurable(private_key, signing_key):
    """An Entra role value cannot carry every name a tenant already uses."""
    client = _client(auth_admin_role="Operador.Ingestao")
    try:
        allowed = _post_ingest(client, _token(private_key, ["Operador.Ingestao"]))
        refused = _post_ingest(client, _token(private_key, ["docquery.admin"]))
    finally:
        app.dependency_overrides.clear()

    assert allowed.status_code == 404
    assert refused.status_code == 403


def test_with_auth_off_ingestion_stays_open(private_key):
    """There is no identity to check, and the quickstart ingests without one.

    Same rule the sector filter follows: auth off means nothing to enforce.
    """
    client = _client(auth_enabled=False, azure_tenant_id="", azure_client_id="")
    try:
        response = client.post("/ingest", json={"path": "docs/definitely-not-here"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
