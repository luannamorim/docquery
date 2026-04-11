from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Document:
    content: str
    metadata: dict[str, str] = field(default_factory=dict)


def load_text(path: Path) -> Document:
    return Document(
        content=path.read_text(encoding="utf-8"),
        metadata={"source": str(path), "file_type": path.suffix},
    )


def load_markdown(path: Path) -> Document:
    return Document(
        content=path.read_text(encoding="utf-8"),
        metadata={"source": str(path), "file_type": ".md"},
    )


def load_pdf(path: Path) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(path)
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return Document(
        content=text,
        metadata={
            "source": str(path),
            "file_type": ".pdf",
            "pages": str(len(reader.pages)),
        },
    )


LOADERS: dict[str, object] = {
    ".txt": load_text,
    ".md": load_markdown,
    ".pdf": load_pdf,
}


def load_document(path: Path) -> Document:
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return loader(path)  # type: ignore[operator]


def load_directory(path: Path) -> list[Document]:
    docs = []
    for file_path in sorted(path.iterdir()):
        if file_path.suffix.lower() in LOADERS:
            docs.append(load_document(file_path))
    return docs
