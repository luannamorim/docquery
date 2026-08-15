import pytest
from pydantic import ValidationError

from docquery.config import Settings


def test_auth_disabled_by_default() -> None:
    settings = Settings()
    assert settings.auth_enabled is False


def test_auth_enabled_requires_tenant_and_client_id() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_enabled=True)
    with pytest.raises(ValidationError):
        Settings(auth_enabled=True, azure_tenant_id="tenant")
    with pytest.raises(ValidationError):
        Settings(auth_enabled=True, azure_client_id="client")


def test_auth_enabled_accepts_complete_config() -> None:
    settings = Settings(
        auth_enabled=True, azure_tenant_id="tenant", azure_client_id="client"
    )
    assert settings.auth_enabled is True
    assert settings.azure_tenant_id == "tenant"
