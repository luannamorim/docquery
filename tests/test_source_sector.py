"""sector_for_source: the sector of a document, within the caller's reach.

The sector filter is applied in the Qdrant query, not checked afterwards — a
source outside the caller's sectors must be indistinguishable from one that
does not exist, the same ambiguity the no_match refusal preserves.
"""

from types import SimpleNamespace

import pytest

from docquery.config import Settings
from docquery.retrieve import lookup


def _settings() -> Settings:
    return Settings(openai_api_key="sk-test")


class StubClient:
    def __init__(self, points=(), collections=("documents",)):
        self._points = list(points)
        self._collections = collections
        self.scroll_calls = []

    def get_collections(self):
        return SimpleNamespace(
            collections=[SimpleNamespace(name=n) for n in self._collections]
        )

    def scroll(self, **kwargs):
        self.scroll_calls.append(kwargs)
        return self._points, None


def _point(sector: str):
    return SimpleNamespace(payload={"sector": sector, "source": "x"})


@pytest.fixture
def use_client(monkeypatch):
    def _use(client):
        monkeypatch.setattr(lookup, "_client", lambda settings: client)
        return client

    return _use


def test_a_source_within_the_callers_sectors_names_its_sector(use_client):
    client = use_client(StubClient(points=[_point("financeiro")]))

    sector = lookup.sector_for_source(
        "data/financeiro/contrato.pdf", _settings(), sectors=["financeiro", "rh"]
    )

    assert sector == "financeiro"
    must = client.scroll_calls[0]["scroll_filter"].must
    assert {c.key for c in must} == {"source", "sector"}


def test_no_sectors_reads_nothing_without_touching_qdrant(use_client):
    client = use_client(StubClient(points=[_point("financeiro")]))

    assert lookup.sector_for_source("data/x.pdf", _settings(), sectors=[]) is None
    assert client.scroll_calls == []


def test_none_sectors_skips_the_sector_condition(use_client):
    client = use_client(StubClient(points=[_point("financeiro")]))

    sector = lookup.sector_for_source("data/x.pdf", _settings(), sectors=None)

    assert sector == "financeiro"
    must = client.scroll_calls[0]["scroll_filter"].must
    assert {c.key for c in must} == {"source"}


def test_blank_sectors_are_dropped_before_the_filter(use_client):
    client = use_client(StubClient(points=[_point("rh")]))

    assert (
        lookup.sector_for_source("data/x.pdf", _settings(), sectors=[""]) is None
    )
    assert client.scroll_calls == []

    lookup.sector_for_source("data/x.pdf", _settings(), sectors=["", "rh"])
    sector_condition = next(
        c for c in client.scroll_calls[0]["scroll_filter"].must if c.key == "sector"
    )
    assert sector_condition.match.any == ["rh"]


def test_an_unknown_source_is_none(use_client):
    use_client(StubClient(points=[]))

    assert (
        lookup.sector_for_source("data/nada.pdf", _settings(), sectors=None) is None
    )


def test_a_missing_collection_is_none_not_an_error(use_client):
    client = use_client(StubClient(points=[_point("rh")], collections=()))

    assert lookup.sector_for_source("data/x.pdf", _settings(), sectors=None) is None
    assert client.scroll_calls == []
