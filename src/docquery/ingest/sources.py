"""Pull documents from remote stores into a local directory.

Ingestion is a one-shot pull: files are downloaded to a temporary directory,
handed to the ordinary loaders (Docling needs a real file on disk) and thrown
away. Nothing here caches or synchronises — re-ingesting simply fetches again,
and the existing source-prefix deduplication decides what changed.

Dispatch mirrors LOADERS in loader.py: a dict from scheme to function, no
classes and no Protocol, because there are only two fetchers and they share one
signature. Anything without a known scheme is a local path and never reaches
this module.

Each fetched file carries the URI it came from as its `source`, so remote
documents flow through clearance/type policies, deduplication and orphan pruning
exactly like local ones.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from docquery.config import Settings
from docquery.ingest.loader import _supported_extensions

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DRIVE_ROOT = "https://www.googleapis.com/drive/v3"

# Docs, Sheets and Slides have no downloadable bytes — they must be exported to
# a concrete format first, which is out of scope for this pull.
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GDRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,}")

_MAX_ATTEMPTS = 3


class SourceError(Exception):
    """A remote source could not be read."""


@dataclass
class FetchedFile:
    """A downloaded file and the URI it should be indexed under."""

    local_path: Path
    source_uri: str


def _http_client(token: str) -> httpx.Client:
    """HTTP client for a fetch run. Replaced wholesale in tests."""
    return httpx.Client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, connect=10.0),
        # Graph answers /content with a 302 to a pre-signed storage URL.
        follow_redirects=True,
    )


# --- HTTP helpers ---------------------------------------------------------


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.is_success:
        return
    # Deliberately built from the status code and our own description of the
    # target: upstream error bodies can echo request details, and the operator
    # needs to know which call failed, not what the remote thought of it.
    if response.status_code in (401, 403):
        raise SourceError(f"access denied ({response.status_code}) reading {what}")
    if response.status_code == 404:
        raise SourceError(f"not found (404): {what}")
    raise SourceError(f"request failed ({response.status_code}) reading {what}")


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(float(response.headers.get("Retry-After", "1")), 30.0)
    except ValueError:
        return 1.0


def _get_json(client: httpx.Client, url: str, what: str, params=None) -> dict:
    """GET a JSON document, retrying while the remote is rate limiting us."""
    for attempt in range(_MAX_ATTEMPTS):
        response = client.get(url, params=params)
        if response.status_code == 429 and attempt < _MAX_ATTEMPTS - 1:
            delay = _retry_after(response)
            logger.warning("Rate limited reading %s; retrying in %.1fs", what, delay)
            time.sleep(delay)
            continue
        _raise_for_status(response, what)
        return response.json()
    raise SourceError(f"rate limited reading {what}")


def _download(
    client: httpx.Client, url: str, dest: Path, max_bytes: int, what: str
) -> bool:
    """Stream a file to dest. Returns False if it exceeded max_bytes.

    Streamed and counted as it arrives because a reported size can be missing or
    wrong, and the ceiling exists to bound what actually lands on disk.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with client.stream("GET", url) as response:
        _raise_for_status(response, what)
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes():
                written += len(chunk)
                if written > max_bytes:
                    handle.close()
                    dest.unlink(missing_ok=True)
                    logger.warning("Skipping %s: exceeds source_max_file_mb", what)
                    return False
                handle.write(chunk)
    return True


def _wanted(name: str, settings: Settings) -> bool:
    return Path(name).suffix.lower() in _supported_extensions(settings)


def _local_name(dest_dir: Path, relative_path: str) -> Path:
    """Flatten a remote path into a unique local filename.

    The relative path is what identifies the document; the local file is scratch
    space that only has to be unique within this run and keep its extension so
    the loaders dispatch correctly.
    """
    suffix = Path(relative_path).suffix
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", relative_path[: -len(suffix) or None])
    candidate = dest_dir / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


# --- SharePoint (Microsoft Graph) -----------------------------------------


def _parse_sharepoint_uri(uri: str) -> tuple[str, str, str, str]:
    """sharepoint://host/sites/<site>/<drive>[/folder] → parts."""
    split = urlsplit(uri)
    parts = [p for p in split.path.split("/") if p]
    if not split.netloc or len(parts) < 3 or parts[0] != "sites":
        raise SourceError(
            "malformed sharepoint URI; expected "
            "sharepoint://<host>/sites/<site>/<drive>[/<folder>]"
        )
    return split.netloc, parts[1], parts[2], "/".join(parts[3:])


def _graph_token(settings: Settings) -> str:
    import msal

    app = msal.ConfidentialClientApplication(
        settings.sharepoint_client_id,
        authority=(
            f"https://login.microsoftonline.com/{settings.sharepoint_tenant_id}"
        ),
        client_credential=settings.sharepoint_client_secret.get_secret_value(),
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        # Only the error code: descriptions from the token endpoint can be long
        # and echo request material.
        raise SourceError(f"SharePoint authentication failed: {result.get('error')}")
    return result["access_token"]


def _graph_drive_id(client: httpx.Client, host: str, site: str, drive: str) -> str:
    site_doc = _get_json(
        client, f"{GRAPH_ROOT}/sites/{host}:/sites/{site}", f"site {site}"
    )
    drives = _get_json(
        client, f"{GRAPH_ROOT}/sites/{site_doc['id']}/drives", f"drives of site {site}"
    )
    for candidate in drives.get("value", []):
        if candidate.get("name") == drive:
            return candidate["id"]
    raise SourceError(f"drive '{drive}' not found in site '{site}'")


def _graph_children_url(drive_id: str, folder: str) -> str:
    base = f"{GRAPH_ROOT}/drives/{drive_id}/root"
    if not folder:
        return f"{base}/children"
    return f"{base}:/{quote(folder)}:/children"


def _list_sharepoint(
    client: httpx.Client, drive_id: str, folder: str, prefix: str
) -> list[tuple[str, str, int]]:
    """Recursively list files as (item_id, relative_path, size)."""
    items: list[tuple[str, str, int]] = []
    url = _graph_children_url(drive_id, folder)
    while url:
        page = _get_json(client, url, f"folder '{folder or '/'}'")
        for entry in page.get("value", []):
            name = entry.get("name", "")
            relative = f"{prefix}/{name}" if prefix else name
            if "folder" in entry:
                items.extend(
                    _list_sharepoint(
                        client,
                        drive_id,
                        f"{folder}/{name}" if folder else name,
                        relative,
                    )
                )
            else:
                items.append((entry["id"], relative, int(entry.get("size", 0))))
        url = page.get("@odata.nextLink")
    return items


def fetch_sharepoint(uri: str, dest_dir: Path, settings: Settings) -> list[FetchedFile]:
    if not (
        settings.sharepoint_tenant_id
        and settings.sharepoint_client_id
        and settings.sharepoint_client_secret
    ):
        raise SourceError("SharePoint credentials are not configured")

    host, site, drive, folder = _parse_sharepoint_uri(uri)
    base_uri = uri.rstrip("/")
    max_bytes = settings.source_max_file_mb * 1024 * 1024
    fetched: list[FetchedFile] = []

    with _http_client(_graph_token(settings)) as client:
        drive_id = _graph_drive_id(client, host, site, drive)
        for item_id, relative, size in _list_sharepoint(client, drive_id, folder, ""):
            if not _wanted(relative, settings):
                logger.debug("Skipping unsupported file: %s", relative)
                continue
            if size > max_bytes:
                logger.warning(
                    "Skipping %s: %d bytes exceeds source_max_file_mb", relative, size
                )
                continue
            local_path = _local_name(dest_dir, relative)
            url = f"{GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/content"
            if _download(client, url, local_path, max_bytes, relative):
                fetched.append(FetchedFile(local_path, f"{base_uri}/{relative}"))

    logger.info("Fetched %d file(s) from %s", len(fetched), uri)
    return fetched


# --- Google Drive ---------------------------------------------------------


def _parse_gdrive_uri(uri: str) -> str:
    """gdrive://<folder id> → folder id.

    Only an id, never a name path: Drive allows duplicate folder names, so a
    name path would be ambiguous. Every folder has a copyable id of its own.
    """
    split = urlsplit(uri)
    if split.path.strip("/"):
        raise SourceError(
            "gdrive URIs take a folder id only (gdrive://<folder id>); "
            "address a subfolder by its own id"
        )
    if not _GDRIVE_ID.fullmatch(split.netloc):
        raise SourceError(f"malformed Google Drive folder id: '{split.netloc}'")
    return split.netloc


def _gdrive_token(settings: Settings) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        str(settings.gdrive_service_account_file),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    credentials.refresh(Request())
    return credentials.token


def _list_gdrive(
    client: httpx.Client, folder_id: str, prefix: str
) -> list[tuple[str, str, int]]:
    """Recursively list files as (file_id, relative_path, size)."""
    entries: list[dict] = []
    page_token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType,size),nextPageToken",
            "pageSize": "200",
        }
        if page_token:
            params["pageToken"] = page_token
        page = _get_json(
            client, f"{DRIVE_ROOT}/files", f"folder {folder_id}", params=params
        )
        entries.extend(page.get("files", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    items: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    # Sorted so that duplicate names are disambiguated the same way on every
    # run — otherwise a re-ingest would rename sources and orphan its own chunks.
    for entry in sorted(entries, key=lambda e: (e.get("name", ""), e["id"])):
        name = entry.get("name", "")
        if entry.get("mimeType") == _GOOGLE_FOLDER_MIME:
            items.extend(
                _list_gdrive(
                    client, entry["id"], f"{prefix}/{name}" if prefix else name
                )
            )
            continue
        if str(entry.get("mimeType", "")).startswith(_GOOGLE_NATIVE_PREFIX):
            logger.warning("Skipping Google-native file (no export): %s", name)
            continue
        if name in seen:
            # Drive permits duplicate names in one folder; keep both indexable.
            stem, suffix = name[: -len(Path(name).suffix) or None], Path(name).suffix
            name = f"{stem}~{entry['id'][:8]}{suffix}"
        seen.add(name)
        relative = f"{prefix}/{name}" if prefix else name
        items.append((entry["id"], relative, int(entry.get("size", 0))))
    return items


def fetch_gdrive(uri: str, dest_dir: Path, settings: Settings) -> list[FetchedFile]:
    if not settings.gdrive_service_account_file:
        raise SourceError("Google Drive credentials are not configured")

    folder_id = _parse_gdrive_uri(uri)
    base_uri = uri.rstrip("/")
    max_bytes = settings.source_max_file_mb * 1024 * 1024
    fetched: list[FetchedFile] = []

    with _http_client(_gdrive_token(settings)) as client:
        for file_id, relative, size in _list_gdrive(client, folder_id, ""):
            if not _wanted(relative, settings):
                logger.debug("Skipping unsupported file: %s", relative)
                continue
            if size > max_bytes:
                logger.warning(
                    "Skipping %s: %d bytes exceeds source_max_file_mb", relative, size
                )
                continue
            local_path = _local_name(dest_dir, relative)
            url = f"{DRIVE_ROOT}/files/{file_id}"
            if _download(client, url, local_path, max_bytes, relative):
                fetched.append(FetchedFile(local_path, f"{base_uri}/{relative}"))

    logger.info("Fetched %d file(s) from %s", len(fetched), uri)
    return fetched


FETCHERS: dict[str, Callable[[str, Path, Settings], list[FetchedFile]]] = {
    "sharepoint": fetch_sharepoint,
    "gdrive": fetch_gdrive,
}


def source_scheme(source: str) -> str | None:
    """Return the remote scheme, or None when source is a local path."""
    scheme = urlsplit(source).scheme
    return scheme if scheme in FETCHERS else None


def fetch(uri: str, dest_dir: Path, settings: Settings) -> list[FetchedFile]:
    """Download everything ingestable under uri into dest_dir."""
    scheme = source_scheme(uri)
    if scheme is None:
        raise SourceError(f"not a remote source URI: {uri}")
    return FETCHERS[scheme](uri, dest_dir, settings)
