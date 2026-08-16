"""Answers come back in the language the question was asked in.

Two separate sources of text, and both were English regardless of the question:
the system prompt that shapes every generated answer, and the three refusals
that never reach the LLM at all (nothing retrieved, nothing indexed, nothing
relevant). A Portuguese question answered in English is a bug in both.
"""

from docquery.config import Settings
from docquery.generate.rag import refusal, system_prompt


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test"}
    defaults.update(overrides)
    return Settings(**defaults)


def test_by_default_the_model_is_told_to_match_the_question():
    """No configuration should be needed for the common case.

    A corpus can hold English contracts read by Portuguese speakers; the answer
    belongs in the reader's language, not the document's.
    """
    prompt = system_prompt(_settings())

    assert "same language" in prompt.lower()


def test_a_configured_language_overrides_the_question():
    prompt = system_prompt(_settings(answer_language="pt-BR"))

    assert "pt-BR" in prompt
    assert "same language" not in prompt.lower()


def test_the_refusals_follow_the_configured_language():
    """These never reach the LLM, so nothing else can translate them."""
    assert "documento" in refusal("no_match", _settings(answer_language="pt-BR"))
    assert "acesso" in refusal("no_match", _settings(answer_language="pt-BR"))


def test_an_unconfigured_language_keeps_the_english_refusals():
    """Without a configured language there is nothing to detect from — the
    refusal is built before any model has seen the question."""
    assert refusal("no_match", _settings()).startswith("No documents matched")


def test_an_unknown_language_falls_back_rather_than_failing():
    """A typo in the env var must not turn every empty result into a KeyError."""
    assert refusal("no_match", _settings(answer_language="klingon")).startswith(
        "No documents matched"
    )


def test_the_compartment_refusal_stays_ambiguous_in_every_language():
    """The sector wording must never confirm that something exists out of reach.

    Translating it is exactly the moment that guarantee could be lost.
    """
    for language in ("", "pt-BR"):
        text = refusal("no_match", _settings(answer_language=language)).lower()
        # It offers two possibilities and commits to neither.
        assert " or " in text or " ou " in text
