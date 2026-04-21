import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from docquery.config import Settings, get_settings


@dataclass
class Document:
    content: str
    metadata: dict[str, str] = field(default_factory=dict)


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
    content = path.read_text(encoding="utf-8")
    content, promoted = _promote_headings(content, settings.heading_patterns)
    file_type = ".md" if promoted else path.suffix
    return Document(
        content=content,
        metadata={"source": str(path), "file_type": file_type},
    )


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
