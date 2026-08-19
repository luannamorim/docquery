"""Serving the built frontend from the API itself.

Same origin, so there is no CORS to configure and no second deployment target —
app.py says CORS is deliberately absent, and this is what keeps that true.

The mount is conditional: the build directory does not exist in a checkout, in
CI, or in the eval environment, and importing the app must not depend on a
frontend having been built.
"""

from fastapi.testclient import TestClient

from docquery.api.app import app, mount_frontend
from docquery.config import Settings, get_settings


def test_the_api_still_answers_when_a_frontend_is_mounted(tmp_path):
    """A catch-all mount at / must not shadow the routes registered before it."""
    (tmp_path / "index.html").write_text("<!doctype html><title>docquery</title>")
    mount_frontend(app, tmp_path)

    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert "docquery" in client.get("/").text


def test_a_missing_build_directory_is_not_an_error(tmp_path):
    """A checkout has no build. Importing the app must not require one."""
    assert mount_frontend(app, tmp_path / "does-not-exist") is False


def test_config_is_reachable_without_a_token():
    """The browser needs it *before* it can obtain one — that is the whole job.

    One of the two deliberate exceptions to "new endpoints require a bearer
    token", alongside /health. It carries only public identifiers: config.py
    already records that tenant and client ids are not secrets.
    """
    settings = Settings(
        auth_enabled=True,
        azure_tenant_id="11111111-1111-1111-1111-111111111111",
        azure_client_id="22222222-2222-2222-2222-222222222222",
        frontend_client_id="33333333-3333-3333-3333-333333333333",
        openai_api_key="sk-test",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        body = TestClient(app).get("/config").json()
    finally:
        app.dependency_overrides.clear()

    assert body == {
        "tenantId": "11111111-1111-1111-1111-111111111111",
        "apiClientId": "22222222-2222-2222-2222-222222222222",
        "clientId": "33333333-3333-3333-3333-333333333333",
        "appName": "docquery",
        "feedbackEnabled": False,
    }


def test_the_app_name_is_configurable():
    """Every deployment brands this for its own company.

    Served rather than compiled in so one image works everywhere — the same
    reason the tenant and client ids are.
    """
    app.dependency_overrides[get_settings] = lambda: Settings(
        openai_api_key="sk-test", app_name="Amaggi Docs"
    )
    try:
        assert TestClient(app).get("/config").json()["appName"] == "Amaggi Docs"
    finally:
        app.dependency_overrides.clear()


def test_config_carries_no_secret():
    """A regression guard worth having: this route is unauthenticated."""
    settings = Settings(
        openai_api_key="sk-super-secret", qdrant_api_key="qdrant-secret"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        raw = TestClient(app).get("/config").text
    finally:
        app.dependency_overrides.clear()

    assert "sk-super-secret" not in raw
    assert "qdrant-secret" not in raw
