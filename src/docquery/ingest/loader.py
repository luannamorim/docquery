import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from docquery.config import Settings, get_settings

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FRONTMATTER_SCAN_LIMIT = 4096  # frontmatter won't exceed 4 KB; bounds backtracking

# Metadata value type carried on documents/chunks. tags is multi-valued.
MetaValue = str | int | list[str]

# Descriptive frontmatter fields authors MAY set. These are non-security:
# they help filtering/citation but do not gate access. Scalars become strings;
# "tags" becomes a list of strings.
_DESCRIPTIVE_SCALARS = ("entity", "title", "effective_date")
# Fields that gate access/scope are classified server-side at ingest time and
# are intentionally NOT read from frontmatter (untrusted authors must not
# self-label). They are logged and dropped if present.
_ACCESS_FIELDS = ("clearance", "doc_type")


@dataclass
class Document:
    content: str
    metadata: dict[str, MetaValue] = field(default_factory=dict)
    # Structured DoclingDocument, set only when the file was parsed by Docling.
    # When present the chunker uses it to keep tables, headings and page
    # provenance; when None the legacy text-based chunking applies unchanged.
    dl_doc: object | None = None


def _parse_frontmatter(text: str) -> tuple[str, dict]:
    """Strip YAML frontmatter and return (body, parsed_dict).

    parsed_dict is the raw key→value mapping from the frontmatter block.
    Falls back to a simple line parser if pyyaml is unavailable.
    """
    m = _FRONTMATTER_RE.match(text[:_FRONTMATTER_SCAN_LIMIT])
    if not m:
        return text, {}
    body = text[m.end() :]
    raw = m.group(1)
    try:
        import yaml  # pyyaml, transitively available via langchain

        parsed = yaml.safe_load(raw) or {}
        return body, parsed if isinstance(parsed, dict) else {}
    except Exception:
        logger.debug(
            "yaml.safe_load failed on frontmatter, falling back to regex", exc_info=True
        )
        parsed = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                parsed[key.strip()] = val.strip()
        return body, parsed


def _descriptive_metadata(parsed: dict, source: str) -> dict[str, MetaValue]:
    """Extract the allowlisted descriptive fields, normalizing their types.

    Access-gating fields (clearance, doc_type) in frontmatter are ignored with
    a warning — they are set server-side by settings policy at ingest time.
    """
    meta: dict[str, MetaValue] = {}
    for key in _DESCRIPTIVE_SCALARS:
        if parsed.get(key) not in (None, ""):
            meta[key] = str(parsed[key])
    tags = parsed.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    if isinstance(tags, list):
        cleaned = [str(t).strip() for t in tags if str(t).strip()]
        if cleaned:
            meta["tags"] = cleaned
    for key in _ACCESS_FIELDS:
        if key in parsed:
            logger.warning(
                "Frontmatter %r on %s is ignored; %s is classified server-side "
                "by settings policy at ingest time",
                key,
                source,
                key,
            )
    return meta


def _promote_headings(text: str, patterns: list[str]) -> tuple[str, bool]:
    """Prefix lines matching any pattern with '## ' so they become MD headers.

    Returns (new_text, promoted) where promoted is True if at least one line
    was rewritten.
    """
    promoted = False
    for pat in patterns:
        rx = re.compile(pat, re.MULTILINE)
        new_text, n = rx.subn(lambda m: f"## {m.group(0)}", text)
        if n:
            promoted = True
            text = new_text
    return text, promoted


def load_text(path: Path, settings: Settings | None = None) -> Document:
    settings = settings or get_settings()
    raw = path.read_text(encoding="utf-8")
    descriptive: dict[str, MetaValue] = {}
    if path.suffix.lower() == ".md":
        raw, parsed = _parse_frontmatter(raw)
        descriptive = _descriptive_metadata(parsed, str(path))
    content, promoted = _promote_headings(raw, settings.heading_patterns)
    file_type = ".md" if promoted else path.suffix
    meta: dict[str, MetaValue] = {
        "source": str(path),
        "file_type": file_type,
        **descriptive,
    }
    return Document(content=content, metadata=meta)


def load_pdf(path: Path, settings: Settings | None = None) -> Document:
    from pypdf import PdfReader

    settings = settings or get_settings()
    reader = PdfReader(path)
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    text, promoted = _promote_headings(text, settings.heading_patterns)
    file_type = ".md" if promoted else ".pdf"
    meta: dict[str, MetaValue] = {
        "source": str(path),
        "file_type": file_type,
        "pages": str(len(reader.pages)),
    }
    return Document(content=text, metadata=meta)


LOADERS: dict[str, Callable[[Path, Settings | None], Document]] = {
    ".txt": load_text,
    ".md": load_text,
    ".pdf": load_pdf,
}


def _supported_extensions(settings: Settings) -> set[str]:
    """Extensions accepted for ingestion under the current settings."""
    extensions = set(LOADERS)
    if settings.docling_enabled:
        from docquery.ingest.docling_loader import DOCLING_EXTENSIONS

        extensions |= DOCLING_EXTENSIONS
    return extensions


def load_document(path: Path, settings: Settings | None = None) -> Document:
    settings = settings or get_settings()

    if settings.docling_enabled:
        from docquery.ingest import docling_loader

        if docling_loader.handles(path, settings):
            try:
                return docling_loader.load_with_docling(path, settings)
            except docling_loader.DoclingLimitExceeded:
                # A configured limit is operator policy, not a broken file:
                # downgrading to the legacy parser would silently ingest the
                # document the limit was meant to keep out.
                raise
            except Exception as e:
                if path.suffix.lower() not in docling_loader.FALLBACK_EXTENSIONS:
                    raise docling_loader.DoclingConversionError(
                        f"Docling could not parse {path}: {e}"
                    ) from e
                logger.warning(
                    "Docling failed on %s (%s); falling back to the legacy parser",
                    path,
                    e,
                )

    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return loader(path, settings)


def iter_ingestable_files(path: Path, settings: Settings | None = None) -> list[Path]:
    """Recursively list supported files under path, sorted, skipping symlinks.

    Recursion lets a single ingest root hold typed subfolders
    (e.g. data/contracts/, data/policies/). `rglob` does not descend into
    symlinked directories, and symlinked files are skipped explicitly.
    """
    settings = settings or get_settings()
    extensions = _supported_extensions(settings)
    files: list[Path] = []
    for file_path in sorted(path.rglob("*")):
        if file_path.is_symlink():
            logger.warning("Skipping symlink during ingest: %s", file_path)
            continue
        if file_path.is_file() and file_path.suffix.lower() in extensions:
            files.append(file_path)
    return files


def load_directory(path: Path, settings: Settings | None = None) -> list[Document]:
    settings = settings or get_settings()
    docs: list[Document] = []
    for file_path in iter_ingestable_files(path, settings):
        try:
            docs.append(load_document(file_path, settings))
        except Exception as e:
            # Only Docling-exclusive formats are skipped: they have no legacy
            # parser, so one unreadable file should not abort the whole run.
            # Legacy formats keep aborting, as they always have.
            if settings.docling_enabled:
                from docquery.ingest.docling_loader import DoclingConversionError

                if isinstance(e, DoclingConversionError):
                    logger.error("Skipping %s: %s", file_path, e)
                    continue
            raise
    return docs
