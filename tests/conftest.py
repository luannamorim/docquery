"""Keep the suite hermetic: it must not read the developer's own .env.

Settings loads .env by default, so any test that builds a bare Settings() —
or drives the app without overriding the dependency — inherits whatever the
machine happens to be configured with. That has broken this suite three times:
a leftover TYPE_POLICY, then the removed clearance keys, then AUTH_ENABLED=true
turning every unauthenticated request into a 401.

None of those were real failures, and worse, the reverse is possible: a test
asserting a closed door could pass only because the local .env happened to
close it. Pinning env_file to None here makes every test read the declared
defaults, the same on any machine and in CI.

Tests that need a configured value pass it explicitly, which they already do.
"""

import pytest

from docquery.config import Settings, get_settings


@pytest.fixture(autouse=True, scope="session")
def _ignore_developer_env_file() -> None:
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
