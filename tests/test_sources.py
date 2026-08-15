"""Orphan-prefix matching and remote source fetching.

The remote fetchers are driven through httpx.MockTransport rather than by
patching module internals, so pagination, streaming downloads and the size cap
run for real against scripted responses and never touch the network.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from docquery.config import Settings
from docquery.ingest import pipeline, sources


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


# --- URI dispatch ---------------------------------------------------------


def test_local_paths_have_no_scheme() -> None:
    assert sources.source_scheme("docs/sample") is None
    assert sources.source_scheme("/abs/docs") is None
    assert sources.source_scheme("./docs") is None


def test_remote_schemes_are_recognised() -> None:
    assert sources.source_scheme("sharepoint://host/sites/Eng/Docs") == "sharepoint"
    assert sources.source_scheme("gdrive://folder-id-1234567890") == "gdrive"


def test_unknown_scheme_is_not_treated_as_remote() -> None:
    """An unknown scheme must not silently fall through to a remote fetcher."""
    assert sources.source_scheme("s3://bucket/key") is None


# --- SharePoint -----------------------------------------------------------

SP_URI = "sharepoint://contoso.sharepoint.com/sites/Eng/Documents/policies"


def _sharepoint_settings(**overrides) -> Settings:
    fields = {
        "sharepoint_tenant_id": "tenant",
        "sharepoint_client_id": "client",
        "sharepoint_client_secret": "secret",
    }
    return Settings(**{**fields, **overrides})


def _sharepoint_handler(children: dict[str, list], contents=None):
    """Serve site → drives → children pages → file content."""
    contents = contents or {}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, request.url.query.decode()
        seen.append(path)
        if path.endswith(":/sites/Eng"):
            return httpx.Response(200, json={"id": "site-1"})
        if path.endswith("/sites/site-1/drives"):
            return httpx.Response(
                200,
                json={"value": [{"id": "drive-1", "name": "Documents"}]},
            )
        if path.endswith("/content"):
            item_id = path.split("/items/")[1].split("/")[0]
            return httpx.Response(200, content=contents.get(item_id, b"body"))
        if "children" in path:
            folder = path.split("root:/")[1].split(":/children")[0]
            page = "2" if "page=2" in query else "1"
            return httpx.Response(200, json=children[f"{folder}#{page}"])
        raise AssertionError(f"unexpected request: {request.url}")

    return handler, seen


@pytest.fixture
def mock_http(monkeypatch):
    """Route sources' HTTP client at a scripted transport."""

    def install(handler):
        def _client(token: str) -> httpx.Client:
            return httpx.Client(
                transport=httpx.MockTransport(handler),
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
            )

        monkeypatch.setattr(sources, "_http_client", _client)
        monkeypatch.setattr(sources, "_graph_token", lambda settings: "graph-token")
        monkeypatch.setattr(sources, "_gdrive_token", lambda settings: "drive-token")

    return install


def test_sharepoint_fetches_files_with_uri_sources(mock_http, tmp_path) -> None:
    handler, _ = _sharepoint_handler(
        {
            "policies#1": {
                "value": [
                    {"id": "i1", "name": "a.md", "size": 10, "file": {}},
                    {"id": "i2", "name": "b.pdf", "size": 20, "file": {}},
                ]
            }
        },
        contents={"i1": b"# a", "i2": b"pdf-bytes"},
    )
    mock_http(handler)
    fetched = sources.fetch(SP_URI, tmp_path, _sharepoint_settings())

    assert [f.source_uri for f in fetched] == [
        f"{SP_URI}/a.md",
        f"{SP_URI}/b.pdf",
    ]
    assert fetched[0].local_path.read_bytes() == b"# a"
    assert all(f.local_path.parent == tmp_path for f in fetched)


def test_sharepoint_recurses_into_subfolders(mock_http, tmp_path) -> None:
    handler, _ = _sharepoint_handler(
        {
            "policies#1": {
                "value": [
                    {"id": "i1", "name": "top.md", "size": 10, "file": {}},
                    {"name": "rh", "folder": {"childCount": 1}},
                ]
            },
            "policies/rh#1": {
                "value": [{"id": "i2", "name": "ferias.md", "size": 10, "file": {}}]
            },
        }
    )
    mock_http(handler)
    fetched = sources.fetch(SP_URI, tmp_path, _sharepoint_settings())

    assert {f.source_uri for f in fetched} == {
        f"{SP_URI}/top.md",
        f"{SP_URI}/rh/ferias.md",
    }


def test_sharepoint_follows_pagination(mock_http, tmp_path) -> None:
    handler, _ = _sharepoint_handler(
        {
            "policies#1": {
                "value": [{"id": "i1", "name": "a.md", "size": 10, "file": {}}],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/drives/drive-1"
                    "/root:/policies:/children?page=2"
                ),
            },
            "policies#2": {
                "value": [{"id": "i2", "name": "b.md", "size": 10, "file": {}}]
            },
        }
    )
    mock_http(handler)
    fetched = sources.fetch(SP_URI, tmp_path, _sharepoint_settings())
    assert len(fetched) == 2


def test_sharepoint_skips_unsupported_extensions(mock_http, tmp_path) -> None:
    handler, _ = _sharepoint_handler(
        {
            "policies#1": {
                "value": [
                    {"id": "i1", "name": "a.md", "size": 10, "file": {}},
                    {"id": "i2", "name": "video.mp4", "size": 10, "file": {}},
                ]
            }
        }
    )
    mock_http(handler)
    fetched = sources.fetch(SP_URI, tmp_path, _sharepoint_settings())
    assert [f.source_uri for f in fetched] == [f"{SP_URI}/a.md"]


def test_oversized_file_is_skipped_not_fatal(mock_http, tmp_path) -> None:
    """One file over the ceiling must not abort the whole folder."""
    handler, _ = _sharepoint_handler(
        {
            "policies#1": {
                "value": [
                    {"id": "i1", "name": "huge.pdf", "size": 99 * 1024 * 1024},
                    {"id": "i2", "name": "small.md", "size": 10, "file": {}},
                ]
            }
        }
    )
    mock_http(handler)
    settings = _sharepoint_settings(source_max_file_mb=1)
    fetched = sources.fetch(SP_URI, tmp_path, settings)
    assert [f.source_uri for f in fetched] == [f"{SP_URI}/small.md"]


def test_missing_credentials_are_rejected(tmp_path) -> None:
    with pytest.raises(sources.SourceError, match="credentials"):
        sources.fetch(SP_URI, tmp_path, Settings())


def test_malformed_sharepoint_uri_is_rejected(tmp_path) -> None:
    with pytest.raises(sources.SourceError):
        sources.fetch("sharepoint://host/Documents", tmp_path, _sharepoint_settings())


def test_permission_denied_reports_without_leaking_secret(mock_http, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    mock_http(handler)
    with pytest.raises(sources.SourceError) as exc:
        sources.fetch(SP_URI, tmp_path, _sharepoint_settings())
    assert "secret" not in str(exc.value)
    assert "403" in str(exc.value) or "denied" in str(exc.value).lower()


def test_unknown_site_reports_clearly(mock_http, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    mock_http(handler)
    with pytest.raises(sources.SourceError):
        sources.fetch(SP_URI, tmp_path, _sharepoint_settings())


def test_rate_limit_is_retried(mock_http, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: None)
    calls = {"n": 0}
    inner, _ = _sharepoint_handler(
        {"policies#1": {"value": [{"id": "i1", "name": "a.md", "size": 5, "file": {}}]}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "children" in request.url.path and calls["n"] == 0:
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        return inner(request)

    mock_http(handler)
    fetched = sources.fetch(SP_URI, tmp_path, _sharepoint_settings())
    assert len(fetched) == 1
    assert calls["n"] == 1


# --- Google Drive ---------------------------------------------------------

GD_URI = "gdrive://folder-abc1234567"


def _gdrive_settings(tmp_path, **overrides) -> Settings:
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"type": "service_account"}))
    return Settings(gdrive_service_account_file=key, **overrides)


def _gdrive_handler(pages: dict[str, dict], contents=None):
    contents = contents or {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path.startswith("/drive/v3/files/"):
            file_id = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, content=contents.get(file_id, b"body"))
        parent = params["q"].split("'")[1]
        key = f"{parent}#{params.get('pageToken', '1')}"
        return httpx.Response(200, json=pages[key])

    return handler


def test_gdrive_fetches_files_with_uri_sources(mock_http, tmp_path) -> None:
    handler = _gdrive_handler(
        {
            "folder-abc1234567#1": {
                "files": [
                    {"id": "f1", "name": "a.md", "mimeType": "text/markdown", "size": 3}
                ]
            }
        },
        contents={"f1": b"# a"},
    )
    mock_http(handler)
    fetched = sources.fetch(GD_URI, tmp_path, _gdrive_settings(tmp_path))
    assert [f.source_uri for f in fetched] == [f"{GD_URI}/a.md"]
    assert fetched[0].local_path.read_bytes() == b"# a"


def test_gdrive_recurses_and_paginates(mock_http, tmp_path) -> None:
    folder_mime = "application/vnd.google-apps.folder"
    handler = _gdrive_handler(
        {
            "folder-abc1234567#1": {
                "files": [
                    {"id": "f1", "name": "a.md", "mimeType": "text/markdown", "size": 3}
                ],
                "nextPageToken": "tok2",
            },
            "folder-abc1234567#tok2": {
                "files": [{"id": "sub", "name": "rh", "mimeType": folder_mime}]
            },
            "sub#1": {
                "files": [
                    {"id": "f2", "name": "b.md", "mimeType": "text/markdown", "size": 3}
                ]
            },
        }
    )
    mock_http(handler)
    fetched = sources.fetch(GD_URI, tmp_path, _gdrive_settings(tmp_path))
    assert {f.source_uri for f in fetched} == {f"{GD_URI}/a.md", f"{GD_URI}/rh/b.md"}


def test_gdrive_skips_google_native_documents(mock_http, tmp_path) -> None:
    """Docs/Sheets have no bytes to download; exporting them is out of scope."""
    handler = _gdrive_handler(
        {
            "folder-abc1234567#1": {
                "files": [
                    {
                        "id": "f1",
                        "name": "notes",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                    {
                        "id": "f2",
                        "name": "a.md",
                        "mimeType": "text/markdown",
                        "size": 3,
                    },
                ]
            }
        }
    )
    mock_http(handler)
    fetched = sources.fetch(GD_URI, tmp_path, _gdrive_settings(tmp_path))
    assert [f.source_uri for f in fetched] == [f"{GD_URI}/a.md"]


def test_gdrive_disambiguates_duplicate_names(mock_http, tmp_path) -> None:
    """Drive allows two files with the same name in one folder; sources must differ."""
    handler = _gdrive_handler(
        {
            "folder-abc1234567#1": {
                "files": [
                    {
                        "id": "f2",
                        "name": "a.md",
                        "mimeType": "text/markdown",
                        "size": 3,
                    },
                    {
                        "id": "f1",
                        "name": "a.md",
                        "mimeType": "text/markdown",
                        "size": 3,
                    },
                ]
            }
        }
    )
    mock_http(handler)
    fetched = sources.fetch(GD_URI, tmp_path, _gdrive_settings(tmp_path))
    uris = [f.source_uri for f in fetched]
    assert len(set(uris)) == 2
    # Deterministic across runs: ordered by (name, id), so f1 keeps the plain name.
    assert uris[0] == f"{GD_URI}/a.md"
    assert "f2" in uris[1]


def test_gdrive_requires_a_service_account_file(tmp_path) -> None:
    with pytest.raises(sources.SourceError, match="credentials"):
        sources.fetch(GD_URI, tmp_path, Settings())


def test_gdrive_rejects_a_path_after_the_folder_id(tmp_path) -> None:
    with pytest.raises(sources.SourceError, match="folder id"):
        sources.fetch(
            "gdrive://folder-abc1234567/subfolder", tmp_path, _gdrive_settings(tmp_path)
        )


# --- URI validation -------------------------------------------------------


def test_validate_accepts_a_well_formed_uri(tmp_path) -> None:
    sources.validate_uri(SP_URI, _sharepoint_settings())
    sources.validate_uri(GD_URI, _gdrive_settings(tmp_path))


def test_validate_rejects_malformed_and_unconfigured(tmp_path) -> None:
    with pytest.raises(sources.SourceError):
        sources.validate_uri("sharepoint://host/Documents", _sharepoint_settings())
    with pytest.raises(sources.SourceError, match="credentials"):
        sources.validate_uri(SP_URI, Settings())
    with pytest.raises(sources.SourceError):
        sources.validate_uri("gdrive://short", _gdrive_settings(tmp_path))


def test_malformed_uri_fails_before_touching_qdrant(monkeypatch) -> None:
    """A bad URI must not surface as a Qdrant connection error."""

    def unreachable(settings):
        raise AssertionError("Qdrant must not be contacted for an invalid URI")

    monkeypatch.setattr(pipeline, "_qdrant_client", unreachable)
    with pytest.raises(sources.SourceError):
        pipeline.ingest_source("gdrive://short", settings=Settings())


# --- allowlist ------------------------------------------------------------


def test_allowlist_matches_the_prefix_and_below() -> None:
    prefixes = ["sharepoint://host/sites/Eng/Docs"]
    assert sources.is_allowed_uri("sharepoint://host/sites/Eng/Docs", prefixes)
    assert sources.is_allowed_uri("sharepoint://host/sites/Eng/Docs/rh", prefixes)


def test_allowlist_respects_path_boundaries() -> None:
    """A prefix must not authorise a sibling that merely starts with it."""
    prefixes = ["sharepoint://host/sites/Eng"]
    assert not sources.is_allowed_uri("sharepoint://host/sites/Engineering", prefixes)


def test_allowlist_is_empty_by_default() -> None:
    assert not sources.is_allowed_uri(SP_URI, [])


def test_relative_segments_are_rejected() -> None:
    """Otherwise an allowlisted prefix could address a folder outside itself."""
    with pytest.raises(sources.SourceError, match="relative"):
        sources.validate_uri(
            "sharepoint://host/sites/Eng/Docs/../../Secret", _sharepoint_settings()
        )


# --- ingest_source routing ------------------------------------------------


@pytest.fixture
def captured_ingest(monkeypatch):
    """Stub out Qdrant so ingest_source can be observed without a server."""
    captured: dict = {}

    def _ingest_documents(docs, client, settings, current_sources, orphan_prefix):
        captured["docs"] = docs
        captured["current_sources"] = current_sources
        captured["orphan_prefix"] = orphan_prefix
        return {"chunks": len(docs), "deleted": 0}

    monkeypatch.setattr(pipeline, "_qdrant_client", lambda settings: object())
    monkeypatch.setattr(pipeline, "ensure_collection", lambda client, settings: None)
    monkeypatch.setattr(pipeline, "_ingest_documents", _ingest_documents)
    return captured


def test_local_path_is_delegated_to_ingest_path(monkeypatch) -> None:
    """A bare path must keep its existing behaviour, unchanged."""
    seen: dict = {}

    def fake_ingest_path(path, settings=None):
        seen["path"] = path
        return {"chunks": 3, "deleted": 0}

    monkeypatch.setattr(pipeline, "ingest_path", fake_ingest_path)
    result = pipeline.ingest_source("docs/sample", settings=Settings())
    assert seen["path"] == Path("docs/sample")
    assert result == {"chunks": 3, "deleted": 0}


def test_remote_documents_are_indexed_under_their_uri(
    captured_ingest, monkeypatch, tmp_path
) -> None:
    downloaded = tmp_path / "scratch.md"
    downloaded.write_text("# hello")
    fetched = [sources.FetchedFile(downloaded, f"{SP_URI}/rh/a.md")]
    monkeypatch.setattr(pipeline, "fetch", lambda uri, dest, settings: fetched)

    result = pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())

    doc = captured_ingest["docs"][0]
    # The scratch file is temporary; the URI is what identifies the document.
    assert doc.metadata["source"] == f"{SP_URI}/rh/a.md"
    assert captured_ingest["current_sources"] == {f"{SP_URI}/rh/a.md"}
    assert captured_ingest["orphan_prefix"] == f"{SP_URI}/"
    assert result["chunks"] == 1


def test_downloads_are_discarded_after_ingest(
    captured_ingest, monkeypatch, tmp_path
) -> None:
    """The temporary directory must not survive the run."""
    holder: dict = {}

    def fake_fetch(uri, dest_dir, settings):
        holder["dest"] = dest_dir
        downloaded = dest_dir / "a.md"
        downloaded.write_text("# hello")
        return [sources.FetchedFile(downloaded, f"{uri}/a.md")]

    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())
    assert not holder["dest"].exists()


def test_remote_fetch_failure_still_cleans_up(
    captured_ingest, monkeypatch, tmp_path
) -> None:
    holder: dict = {}

    def failing_fetch(uri, dest_dir, settings):
        holder["dest"] = dest_dir
        raise sources.SourceError("boom")

    monkeypatch.setattr(pipeline, "fetch", failing_fetch)
    with pytest.raises(sources.SourceError):
        pipeline.ingest_source(SP_URI, settings=_sharepoint_settings())
    assert not holder["dest"].exists()
