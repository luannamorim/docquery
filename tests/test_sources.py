"""Orphan-prefix matching and remote source fetching."""

from pathlib import Path
from types import SimpleNamespace

from docquery.config import Settings
from docquery.ingest import pipeline


class FakeQdrant:
    """Minimal stand-in that records deletes and replays one page of points."""

    def __init__(self, sources: list[str]) -> None:
        self._points = [
            SimpleNamespace(payload={"source": source}) for source in sources
        ]
        self.deleted: list[str] = []

    def scroll(self, **kwargs):
        return self._points, None

    def delete(self, collection_name, points_selector):
        condition = points_selector.filter.must[0]
        self.deleted.append(condition.match.value)


def test_sibling_directory_is_not_treated_as_orphan() -> None:
    """Ingesting docs/sample must not claim docs/sample-old as its own.

    The prefix has to be bounded by a separator: without it, a sibling whose
    name merely starts with the ingested directory's name looks like an orphan
    and its chunks get deleted.
    """
    client = FakeQdrant(["docs/sample/a.md", "docs/sample-old/b.md"])
    deleted = pipeline.delete_orphan_chunks(
        client,
        Settings(),
        pipeline.orphan_prefix_for(Path("docs/sample")),
        {"docs/sample/a.md"},
    )
    assert deleted == 0
    assert client.deleted == []


def test_orphan_prefix_is_bounded_by_a_separator() -> None:
    assert pipeline.orphan_prefix_for(Path("docs/sample")) == "docs/sample/"
    assert pipeline.orphan_prefix_for("gdrive://folder-id") == "gdrive://folder-id/"
    assert pipeline.orphan_prefix_for("gdrive://folder-id/") == "gdrive://folder-id/"


def test_missing_source_under_prefix_is_deleted() -> None:
    client = FakeQdrant(["docs/sample/a.md", "docs/sample/gone.md"])
    deleted = pipeline.delete_orphan_chunks(
        client, Settings(), "docs/sample/", {"docs/sample/a.md"}
    )
    assert deleted == 1
    assert client.deleted == ["docs/sample/gone.md"]
