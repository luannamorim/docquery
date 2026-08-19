"""When a document was last updated, read from the document itself.

The filesystem's mtime is deliberately not a source here. It records the last
write to *this copy*, so in a corpus that arrives by sync, by `docker cp` or by
checkout it holds the date of the copy — several files under this repo's own
`docs/` share one mtime down to the sub-second for exactly that reason. Labelling
that as "updated" would answer the question wrongly instead of leaving it open.

What survives a copy is the timestamp the editor wrote inside the file: PDF
document information, OOXML core properties. When the file carries none, there is
no date, and the caller says so rather than substituting one. A remote library
that records the edit itself (SharePoint, Drive) is a better source still and
overrides this one in `ingest_source`.
"""

import logging
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

# Core properties come from a document the operator does not control, so the
# parser must not resolve entities or expand a DTD. defusedxml arrives with
# docling; it is declared in pyproject because this module depends on it.
from defusedxml.ElementTree import fromstring

logger = logging.getLogger(__name__)

# OOXML keeps the last-save timestamp in the package's core properties, written
# by the editor on every save.
_CORE_PROPERTIES = "docProps/core.xml"
_DCTERMS_MODIFIED = "{http://purl.org/dc/terms/}modified"


def to_utc_iso(value: str | datetime | None) -> str:
    """Normalize a timestamp to a UTC RFC 3339 string, or "" if unusable.

    Everything is stored in UTC so two documents edited at the same instant
    compare equal whatever offset their producers wrote.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            logger.debug("Ignoring unparseable modification date %r", value)
            return ""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        # A timestamp with no offset is ambiguous. Reading it as UTC keeps a
        # real edit date usable and is wrong by the producer's offset at worst;
        # dropping it would lose the only date the file has.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _pdf_modified_at(path: Path) -> str:
    from pypdf import PdfReader

    info = PdfReader(path).metadata
    # A scanned or exported PDF carries the date of the scan/export, which is
    # still when that document was produced — not when it was ingested.
    return to_utc_iso(info.modification_date if info else None)


def _ooxml_modified_at(path: Path) -> str:
    with zipfile.ZipFile(path) as package:
        try:
            core = package.read(_CORE_PROPERTIES)
        except KeyError:
            return ""
    modified = fromstring(core).find(_DCTERMS_MODIFIED)
    return to_utc_iso(modified.text if modified is not None else None)


# Formats whose containers record a last-save timestamp. Plain text and Markdown
# record nothing, so they have no entry and no date.
_READERS: dict[str, Callable[[Path], str]] = {
    ".pdf": _pdf_modified_at,
    ".docx": _ooxml_modified_at,
    ".xlsx": _ooxml_modified_at,
    ".pptx": _ooxml_modified_at,
}


def embedded_modified_at(path: Path) -> str:
    """The document's own last-modified timestamp, or "" when it carries none.

    Never raises: a missing date is a nicety, and a malformed one is not worth
    failing an ingest over.
    """
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        return ""
    try:
        return reader(path)
    except Exception:
        logger.debug("No usable modification date in %s", path, exc_info=True)
        return ""
