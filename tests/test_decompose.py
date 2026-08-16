"""Splitting a question that asks for two things at once.

Measured on the real corpus: "qual o prazo do contrato da CRK e o valor?" scored
3.37 at best, where "qual o prazo" alone scored 4.80 and "qual o valor" alone
scored 5.52. No single passage answers both, so the cross-encoder — which scores
each passage against the whole question — rates every candidate poorly and the
answer comes back as "não há informações suficientes".

The split is only half the fix. The other half lives in the pipeline: each part
must be reranked against *itself*. A cross-encoder still shown "prazo e valor"
would score everything badly no matter how the retrieval was divided.
"""

from types import SimpleNamespace

from docquery.config import Settings
from docquery.generate.decompose import decompose


class ExplodingClient:
    """Any attribute access means the LLM was called when it should not be."""

    def __getattr__(self, name):
        raise AssertionError(f"the LLM was called ({name}) with decomposition off")


class FakeClient:
    """Replays a scripted completion and records the request."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _settings(**overrides) -> Settings:
    defaults = {"openai_api_key": "sk-test", "query_decompose_enabled": True}
    defaults.update(overrides)
    return Settings(**defaults)


def test_disabled_by_default_and_never_calls_the_model():
    """Opt-in like every other cost this project adds — and the switch is what
    lets the eval measure the feature against itself."""
    assert Settings(openai_api_key="sk-test").query_decompose_enabled is False

    question = "qual o prazo e o valor?"
    assert decompose(
        question, Settings(openai_api_key="sk-test"), ExplodingClient()
    ) == [question]


def test_a_compound_question_becomes_its_parts():
    client = FakeClient(
        "qual o prazo do contrato da CRK?\nqual o valor do contrato da CRK?"
    )

    parts = decompose("qual o prazo do contrato da CRK e o valor?", _settings(), client)

    assert parts == [
        "qual o prazo do contrato da CRK?",
        "qual o valor do contrato da CRK?",
    ]


def test_a_simple_question_comes_back_as_one_part():
    """The model is told to return the question unchanged when there is nothing
    to separate, so a simple question costs a call and nothing else."""
    client = FakeClient("qual o prazo do contrato da CRK?")

    parts = decompose("qual o prazo do contrato da CRK?", _settings(), client)

    assert parts == ["qual o prazo do contrato da CRK?"]


def test_the_number_of_parts_is_capped():
    """Each part costs a retrieval and a rerank, so the cost has a ceiling."""
    client = FakeClient("um?\ndois?\ntres?\nquatro?\ncinco?")

    parts = decompose("pergunta enorme", _settings(query_decompose_max_parts=3), client)

    assert parts == ["um?", "dois?", "tres?"]


def test_blank_lines_and_list_markers_are_stripped():
    """Models like to number things even when asked not to."""
    client = FakeClient("1. qual o prazo?\n\n- qual o valor?\n  \n2) qual a multa?")

    parts = decompose("prazo, valor e multa?", _settings(), client)

    assert parts == ["qual o prazo?", "qual o valor?", "qual a multa?"]


def test_a_failed_split_falls_back_to_the_whole_question():
    """A decomposition that errors must not take the question down with it —
    the same rule the follow-up rewrite follows."""

    class Broken(FakeClient):
        def create(self, **kwargs):
            raise RuntimeError("upstream is down")

    question = "qual o prazo e o valor?"
    assert decompose(question, _settings(), Broken("")) == [question]


def test_an_empty_reply_falls_back_too():
    assert decompose("qual o prazo?", _settings(), FakeClient("   ")) == [
        "qual o prazo?"
    ]
