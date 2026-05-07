"""Tests for per-query token tracking and cost calculation in generate_answer."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from docquery.config import Settings
from docquery.generate.rag import generate_answer


def _make_response(prompt_tokens: int, completion_tokens: int, content: str = "answer"):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], model="gpt-4o-mini", usage=usage)


def _settings(**overrides) -> Settings:
    defaults = {
        "openai_api_key": "sk-test",
        "llm_model": "gpt-4o-mini",
        "llm_temperature": 0.0,
        "llm_max_tokens": 1024,
        "llm_price_input_per_1m": 0.15,
        "llm_price_output_per_1m": 0.60,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _contexts():
    return [{"source": "doc.md", "chunk_index": 0, "score": 0.9, "text": "ctx", "section": ""}]


@patch("docquery.generate.rag.OpenAI")
def test_cost_fields_present(mock_openai_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _make_response(100, 50)
    settings = _settings()

    result = generate_answer("q?", _contexts(), settings, client)

    assert result["tokens_in"] == 100
    assert result["tokens_out"] == 50
    assert abs(result["cost_usd"] - (100 * 0.15 + 50 * 0.60) / 1_000_000) < 1e-10


@patch("docquery.generate.rag.OpenAI")
def test_cost_zero_when_no_usage(mock_openai_cls):
    client = MagicMock()
    response = _make_response(0, 0)
    response.usage = None
    client.chat.completions.create.return_value = response
    settings = _settings()

    result = generate_answer("q?", _contexts(), settings, client)

    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["cost_usd"] == 0.0


@patch("docquery.generate.rag.OpenAI")
def test_custom_price_config(mock_openai_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _make_response(1_000_000, 1_000_000)
    settings = _settings(llm_price_input_per_1m=1.0, llm_price_output_per_1m=2.0)

    result = generate_answer("q?", _contexts(), settings, client)

    assert abs(result["cost_usd"] - 3.0) < 1e-9
