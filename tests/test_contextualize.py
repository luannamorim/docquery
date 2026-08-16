"""Resolving a follow-up question against the turns before it.

A stateless /query embeds whatever string it is given, so "e a multa por
atraso?" reaches retrieval with no anchor at all — no "contrato", no "Acme" —
and matches whatever happens to share the word. These tests cover the step that
puts the anchor back, and the two properties that keep it from costing more
than it gives: it is skipped entirely on a first turn, and it never reads the
text of an answer.
"""

from docquery.config import Settings
from docquery.generate.contextualize import contextualize


class ExplodingClient:
    """Any attribute access means the LLM was called when it should not be."""

    def __getattr__(self, name):
        raise AssertionError(f"the LLM was called ({name}) with nothing to resolve")


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test"}
    defaults.update(overrides)
    return Settings(**defaults)


class FakeClient:
    """Captures the request and replays a scripted completion."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _Msg:
            content = self.reply

        class _Choice:
            message = _Msg()

        class _Response:
            choices = [_Choice()]

        return _Response()


def test_a_first_turn_is_never_rewritten():
    """No history means nothing to resolve, so the query goes through untouched.

    This is what keeps the existing RAGAS baseline valid: a request without a
    conversation_id runs the exact pipeline it ran before, with no extra call
    and no extra token.
    """
    assert (
        contextualize("Qual o prazo de pagamento?", [], _settings(), ExplodingClient())
        == "Qual o prazo de pagamento?"
    )


def test_a_follow_up_is_resolved_into_a_standalone_question():
    client = FakeClient("multa por atraso no contrato Acme Supply 2024")

    resolved = contextualize(
        "e a multa por atraso?",
        ["Qual o prazo de pagamento do contrato Acme Supply 2024?"],
        _settings(),
        client,
    )

    assert resolved == "multa por atraso no contrato Acme Supply 2024"
    assert len(client.calls) == 1


def test_the_previous_questions_are_what_the_model_sees():
    client = FakeClient("reescrita")

    contextualize(
        "e a multa?",
        ["Qual o prazo do contrato Acme?", "E o reajuste?"],
        _settings(),
        client,
    )

    sent = str(client.calls[0]["messages"])
    assert "Qual o prazo do contrato Acme?" in sent
    assert "E o reajuste?" in sent
    assert "e a multa?" in sent
