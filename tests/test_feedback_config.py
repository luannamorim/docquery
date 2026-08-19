"""feedback_enabled fails fast when it cannot work, like history_enabled."""

import pytest

from docquery.config import Settings

AUTH = {
    "auth_enabled": True,
    "azure_tenant_id": "11111111-1111-1111-1111-111111111111",
    "azure_client_id": "22222222-2222-2222-2222-222222222222",
}


def test_feedback_requires_a_dsn():
    with pytest.raises(ValueError, match="history_dsn"):
        Settings(feedback_enabled=True, **AUTH)


def test_feedback_requires_auth():
    with pytest.raises(ValueError, match="auth_enabled"):
        Settings(
            feedback_enabled=True,
            history_dsn="mysql://user:pass@localhost/docquery",
        )


def test_feedback_boots_with_auth_and_a_dsn():
    settings = Settings(
        feedback_enabled=True,
        history_dsn="mysql://user:pass@localhost/docquery",
        **AUTH,
    )
    assert settings.feedback_enabled is True
