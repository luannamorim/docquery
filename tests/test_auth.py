"""Entra ID token validation and role→clearance mapping.

Tokens are minted locally with an RSA keypair and `auth._get_signing_key` is
monkeypatched to return the matching public key, so the suite exercises the real
jwt.decode path (signature, audience, issuer, expiry, algorithm pinning) without
ever reaching the tenant's JWKS endpoint.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from docquery.api import app as app_module
from docquery.api import auth
from docquery.api.app import app
from docquery.config import Settings, get_settings

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


def _auth_settings(**overrides) -> Settings:
    fields = {
        "auth_enabled": True,
        "azure_tenant_id": TENANT,
        "azure_client_id": CLIENT,
        "auth_role_clearance_map": [("clearance.5", 5), ("clearance.10", 10)],
    }
    return Settings(**{**fields, **overrides})


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_key(monkeypatch, private_key):
    """Resolve every token against private_key's public half."""
    public_key = private_key.public_key()
    monkeypatch.setattr(auth, "_get_signing_key", lambda token, settings: public_key)
    return public_key


def make_token(
    private_key,
    *,
    roles=None,
    aud=CLIENT,
    iss=ISSUER,
    expires_in=3600,
    algorithm="RS256",
    key=None,
    **extra_claims,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "sub": "user-1",
        **extra_claims,
    }
    if roles is not None:
        claims["roles"] = roles
    return jwt.encode(
        claims,
        key if key is not None else private_key,
        algorithm=algorithm,
        headers={"kid": "test-kid"},
    )


# --- Settings validation ---------------------------------------------------


def test_auth_disabled_by_default() -> None:
    assert Settings().auth_enabled is False


def test_auth_enabled_requires_tenant_and_client_id() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_enabled=True)
    with pytest.raises(ValidationError):
        Settings(auth_enabled=True, azure_tenant_id=TENANT)
    with pytest.raises(ValidationError):
        Settings(auth_enabled=True, azure_client_id=CLIENT)


def test_auth_enabled_accepts_complete_config() -> None:
    settings = _auth_settings()
    assert settings.auth_enabled is True
    assert settings.azure_tenant_id == TENANT


# --- validate_token -------------------------------------------------------


def test_valid_token_returns_claims(private_key, signing_key) -> None:
    token = make_token(private_key, roles=["clearance.5"])
    claims = auth.validate_token(token, _auth_settings())
    assert claims["sub"] == "user-1"
    assert claims["roles"] == ["clearance.5"]


def test_app_id_uri_audience_is_accepted(private_key, signing_key) -> None:
    token = make_token(private_key, aud=f"api://{CLIENT}")
    claims = auth.validate_token(token, _auth_settings())
    assert claims["aud"] == f"api://{CLIENT}"


def test_expired_token_is_rejected(private_key, signing_key) -> None:
    token = make_token(private_key, expires_in=-3600)
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"


def test_wrong_audience_is_rejected(private_key, signing_key) -> None:
    token = make_token(private_key, aud="some-other-app")
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert exc.value.status_code == 401


def test_wrong_issuer_is_rejected(private_key, signing_key) -> None:
    token = make_token(private_key, iss="https://sts.windows.net/tenant/")
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert exc.value.status_code == 401


def test_token_signed_by_another_key_is_rejected(
    private_key, other_private_key, signing_key
) -> None:
    token = make_token(private_key, key=other_private_key)
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert exc.value.status_code == 401


def test_symmetric_algorithm_is_rejected(private_key, signing_key) -> None:
    """A token signed HS256 must not validate against the RSA public key."""
    token = make_token(private_key, algorithm="HS256", key="x" * 32)
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert exc.value.status_code == 401


def test_token_without_exp_is_rejected(private_key, signing_key) -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {"aud": CLIENT, "iss": ISSUER, "iat": now, "sub": "user-1"},
        private_key,
        algorithm="RS256",
    )
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert exc.value.status_code == 401


def test_error_detail_does_not_leak_expected_values(private_key, signing_key) -> None:
    token = make_token(private_key, aud="some-other-app")
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert CLIENT not in str(exc.value.detail)
    assert ISSUER not in str(exc.value.detail)


def test_unreachable_jwks_returns_503(monkeypatch, private_key) -> None:
    """A JWKS fetch failure is a server problem, not a bad token."""

    def boom(token, settings):
        raise jwt.PyJWKClientConnectionError("no route to host")

    monkeypatch.setattr(auth, "_get_signing_key", boom)
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(make_token(private_key), _auth_settings())
    assert exc.value.status_code == 503


def test_unknown_kid_is_rejected_as_bad_token(monkeypatch, private_key) -> None:
    """No signing key for the token's kid means a bad token (401), not an outage."""

    def no_matching_key(token, settings):
        raise jwt.PyJWKClientError('Unable to find a signing key that matches: "x"')

    monkeypatch.setattr(auth, "_get_signing_key", no_matching_key)
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(make_token(private_key), _auth_settings())
    assert exc.value.status_code == 401


def test_invalid_token_response_carries_www_authenticate(
    private_key, signing_key
) -> None:
    token = make_token(private_key, expires_in=-3600)
    with pytest.raises(HTTPException) as exc:
        auth.validate_token(token, _auth_settings())
    assert 'Bearer error="invalid_token"' in exc.value.headers["WWW-Authenticate"]


def test_expiry_within_leeway_is_accepted(private_key, signing_key) -> None:
    """Clock drift up to auth_leeway_seconds must not produce spurious 401s."""
    token = make_token(private_key, expires_in=-30)
    claims = auth.validate_token(token, _auth_settings(auth_leeway_seconds=60))
    assert claims["sub"] == "user-1"


# --- roles_to_clearance ---------------------------------------------------


def test_mapped_role_sets_clearance() -> None:
    assert auth.roles_to_clearance(["clearance.5"], _auth_settings()) == 5


def test_highest_mapped_role_wins() -> None:
    roles = ["clearance.5", "clearance.10"]
    assert auth.roles_to_clearance(roles, _auth_settings()) == 10


def test_unmapped_roles_fall_back_to_default() -> None:
    settings = _auth_settings(default_clearance_level=1)
    assert auth.roles_to_clearance(["Reader.All"], settings) == 1


def test_missing_roles_claim_falls_back_to_default() -> None:
    settings = _auth_settings(default_clearance_level=2)
    assert auth.roles_to_clearance([], settings) == 2


def test_clearance_is_capped_at_max() -> None:
    settings = _auth_settings(
        auth_role_clearance_map=[("clearance.99", 99)], max_clearance_level=10
    )
    assert auth.roles_to_clearance(["clearance.99"], settings) == 10


# --- roles_to_sectors -----------------------------------------------------


def _sector_settings(**overrides) -> Settings:
    fields = {
        "auth_role_sector_map": [
            ("sector.rh", "rh"),
            ("sector.juridico", "juridico"),
            ("sector.institucional", "institucional"),
        ]
    }
    return _auth_settings(**{**fields, **overrides})


def test_mapped_role_grants_its_sector() -> None:
    assert auth.roles_to_sectors(["sector.rh"], _sector_settings()) == ["rh"]


def test_sectors_are_the_union_of_the_mapped_roles() -> None:
    roles = ["sector.juridico", "sector.rh"]
    assert auth.roles_to_sectors(roles, _sector_settings()) == ["juridico", "rh"]


def test_two_roles_onto_one_sector_do_not_duplicate_it() -> None:
    """A shared folder is granted by handing the same sector to several roles."""
    settings = _sector_settings(
        auth_role_sector_map=[("sector.rh", "rh"), ("rh.legacy", "rh")]
    )
    assert auth.roles_to_sectors(["sector.rh", "rh.legacy"], settings) == ["rh"]


def test_sector_names_are_normalized_like_folder_names() -> None:
    settings = _sector_settings(auth_role_sector_map=[("sector.rh", "  RH  ")])
    assert auth.roles_to_sectors(["sector.rh"], settings) == ["rh"]


def test_unmapped_role_reads_nothing() -> None:
    """No floor to fall back to — unlike a clearance level, [] means nothing."""
    assert auth.roles_to_sectors(["Reader.All"], _sector_settings()) == []


def test_missing_roles_claim_reads_nothing() -> None:
    assert auth.roles_to_sectors([], _sector_settings()) == []


# --- Endpoint protection --------------------------------------------------


@pytest.fixture
def auth_client(monkeypatch, private_key):
    """TestClient with auth switched on and the JWKS lookup stubbed out."""
    public_key = private_key.public_key()
    monkeypatch.setattr(auth, "_get_signing_key", lambda token, settings: public_key)
    # A zero-argument callable: FastAPI reads the override's signature, so a
    # function with **kwargs would turn them into request parameters.
    app.dependency_overrides[get_settings] = lambda: _auth_settings()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_settings, None)


def _capturing_pipeline() -> tuple[dict, callable]:
    captured: dict = {}

    def _pipeline(query: str, settings=None, user_clearance: int = 0, **kwargs) -> dict:
        captured["user_clearance"] = user_clearance
        return {
            "answer": "test",
            "sources": [],
            "query": query,
            "model": "gpt-4o-mini",
        }

    return captured, _pipeline


def test_query_with_valid_token_uses_role_clearance(auth_client, private_key) -> None:
    token = make_token(private_key, roles=["clearance.5"])
    captured, pipeline = _capturing_pipeline()
    with patch("docquery.api.routes.query_pipeline", side_effect=pipeline):
        response = auth_client.post(
            "/query",
            json={"query": "what is hybrid search?"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert captured["user_clearance"] == 5


def test_query_without_token_is_unauthorized(auth_client) -> None:
    response = auth_client.post("/query", json={"query": "anything"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_query_with_malformed_scheme_is_unauthorized(auth_client) -> None:
    response = auth_client.post(
        "/query",
        json={"query": "anything"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


def test_health_needs_no_token(auth_client) -> None:
    """The Docker healthcheck has no token to present."""
    response = auth_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ingest_without_token_is_unauthorized(auth_client) -> None:
    response = auth_client.post("/ingest", json={"path": "docs"})
    assert response.status_code == 401


def test_ingest_status_without_token_is_unauthorized(auth_client) -> None:
    response = auth_client.get("/ingest/some-task-id")
    assert response.status_code == 401


def test_clearance_header_is_ignored_when_auth_is_on(auth_client, private_key) -> None:
    """A caller must not raise their own clearance past what the token grants."""
    token = make_token(private_key, roles=["clearance.5"])
    captured, pipeline = _capturing_pipeline()
    with patch("docquery.api.routes.query_pipeline", side_effect=pipeline):
        response = auth_client.post(
            "/query",
            json={"query": "what is hybrid search?"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-User-Clearance": "10",
            },
        )
    assert response.status_code == 200
    assert captured["user_clearance"] == 5


def test_token_without_roles_gets_default_clearance(auth_client, private_key) -> None:
    token = make_token(private_key)
    captured, pipeline = _capturing_pipeline()
    with patch("docquery.api.routes.query_pipeline", side_effect=pipeline):
        response = auth_client.post(
            "/query",
            json={"query": "what is hybrid search?"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert captured["user_clearance"] == 0


def test_startup_survives_an_unreachable_jwks_endpoint(monkeypatch) -> None:
    """A briefly unreachable tenant must not stop the app from serving /health."""
    monkeypatch.setattr(app_module, "_get_model", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "_get_reranker", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "get_settings", lambda: _auth_settings())

    def unreachable(jwks_uri):
        raise jwt.PyJWKClientConnectionError("no route to host")

    monkeypatch.setattr(app_module, "_get_jwks_client", unreachable)
    app.dependency_overrides[get_settings] = lambda: _auth_settings()
    try:
        with TestClient(app) as started:
            assert started.get("/health").status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_expired_token_is_unauthorized_at_the_endpoint(
    auth_client, private_key
) -> None:
    token = make_token(private_key, roles=["clearance.5"], expires_in=-3600)
    response = auth_client.post(
        "/query",
        json={"query": "anything"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
