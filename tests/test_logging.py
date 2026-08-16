"""The application's own log lines have to reach the operator.

uvicorn configures its own loggers and leaves the root at WARNING with no
handler, so every logger.info in this codebase is dropped by default — which
silently removes the one line that explains an empty answer:

    Query authorized for sectors=['contracts']

An access-controlled system that cannot tell you which compartment it applied
is one you have to guess about, so the level is configured explicitly.
"""

import logging

from docquery.api.app import configure_logging
from docquery.config import Settings


def test_application_info_lines_are_emitted_by_default():
    """Not the uvicorn default of WARNING: INFO is where the audit trail lives."""
    configure_logging(Settings(openai_api_key="sk-test"))

    assert logging.getLogger("docquery.api.routes").isEnabledFor(logging.INFO)
    assert logging.getLogger("docquery.generate.rag").isEnabledFor(logging.INFO)


def test_the_level_is_configurable():
    """A noisy deployment can turn it down without editing code."""
    configure_logging(Settings(openai_api_key="sk-test", log_level="WARNING"))
    try:
        assert not logging.getLogger("docquery.api.routes").isEnabledFor(logging.INFO)
        assert logging.getLogger("docquery.api.routes").isEnabledFor(logging.WARNING)
    finally:
        configure_logging(Settings(openai_api_key="sk-test"))


def test_configuring_twice_does_not_stack_handlers():
    """The lifespan may run more than once in a process (tests, reloads)."""
    configure_logging(Settings(openai_api_key="sk-test"))
    before = len(logging.getLogger("docquery").handlers)
    configure_logging(Settings(openai_api_key="sk-test"))

    assert len(logging.getLogger("docquery").handlers) == before
