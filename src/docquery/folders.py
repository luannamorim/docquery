"""Folder facets derived from a document's path relative to its ingested root.

Folders are the taxonomy: whatever structure the corpus already has — sectors at
the top of a SharePoint library, subject folders below them — becomes filterable
without any configuration. A new folder is a new facet on the next ingest.

Lives outside ingest/ so retrieval can normalize query-side folder names without
importing the ingest pipeline (and with it qdrant, loaders and remote sources).
"""

import unicodedata


def normalize_segment(segment: str) -> str:
    """Normalize one folder name for exact-match filtering.

    NFC so a decomposed path (macOS local ingest) matches the composed name a
    caller types; lowercase because folder casing is a display choice, not an
    identity. Spaces and accents are otherwise preserved — the payload is
    keyword-matched, so any further slugging would force API callers to
    reproduce the slug rules instead of the folder name they see.
    """
    return unicodedata.normalize("NFC", segment).strip().lower()


def folder_segments(relative_path: str) -> list[str]:
    """Folder parts of a root-relative path, file name dropped, normalized.

    Returns [] for a file sitting directly at the ingested root.
    """
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    return [s for p in parts[:-1] if (s := normalize_segment(p))]
