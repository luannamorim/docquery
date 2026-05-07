import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from docquery.config import Settings, get_settings

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FRONTMATTER_SCAN_LIMIT = 4096  # frontmatter won't exceed 4 KB; bounds backtracking


@dataclass
class Document:
    content: str
    metadata: dict[str, str | int] = field(default_factory=dict)


def _parse_frontmatter(text: str) -> tuple[str, dict[str, int]]:
    """Strip YAML frontmatter and return (body, {key: int_value}) for integer fields.

    Supports only a single level of key: value pairs where value is an integer.
    Falls back to regex if pyyaml is not installed.
    """
    m = _FRONTMATTER_RE.match(text[:_FRONTMATTER_SCAN_LIMIT])
    if not m:
        return text, {}
    body = text[m.end() :]
    raw = m.group(1)
    meta: dict[str, int] = {}
    try:
        import yaml  # pyyaml, transitively available via langchain

        parsed = yaml.safe_load(raw) or {}
        meta = {k: int(v) for k, v in parsed.items() if isinstance(v, (int, float))}
    except Exception:
        logger.debug(
            "yaml.safe_load failed on frontmatter, falling back to regex", exc_info=True
        )
        for line in raw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                try:
                    meta[key.strip()] = int(val.strip())
                except ValueError:
                    pass
    return body, meta


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
    fm_meta: dict[str, int] = {}
    if path.suffix.lower() == ".md":
        raw, fm_meta = _parse_frontmatter(raw)
    content, promoted = _promote_headings(raw, settings.heading_patterns)
    file_type = ".md" if promoted else path.suffix
    meta: dict[str, str | int] = {"source": str(path), "file_type": file_type}
    if "clearance" in fm_meta:
        meta["clearance_level"] = fm_meta["clearance"]
    return Document(content=content, metadata=meta)


def load_pdf(path: Path, settings: Settings | None = None) -> Document:
    from pypdf import PdfReader

    settings = settings or get_settings()
    reader = PdfReader(path)
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    text, promoted = _promote_headings(text, settings.heading_patterns)
    file_type = ".md" if promoted else ".pdf"
    return Document(
        content=text,
        metadata={
            "source": str(path),
            "file_type": file_type,
            "pages": str(len(reader.pages)),
        },
    )


LOADERS: dict[str, Callable[[Path, Settings | None], Document]] = {
    ".txt": load_text,
    ".md": load_text,
    ".pdf": load_pdf,
}


def load_document(path: Path, settings: Settings | None = None) -> Document:
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return loader(path, settings)


def load_directory(path: Path, settings: Settings | None = None) -> list[Document]:
    docs = []
    for file_path in sorted(path.iterdir()):
        if file_path.suffix.lower() in LOADERS:
            docs.append(load_document(file_path, settings))
    return docs
