"""Read what the author highlighted in a PDF, as a signal — never as text.

Word exports text highlighting as filled rectangles drawn in the content
stream behind the glyphs, not as /Highlight annotations; Acrobat and Google
Docs produce annotations. Both paths land here: yellow marks section titles
and critical procedure values, green marks states ("solucionado"). The
extracted spans join the lexical index and the chunk metadata through the
same mechanism `document_terms` uses — the passage stored, cited and sent to
the model stays exactly what the document says.

pdfplumber is imported lazily (the docling precedent): the package never
loads while EMPHASIS_EXTRACTION_ENABLED is off.

On the legacy PDF path a full-line CAPS (or noticeably larger) yellow
highlight is promoted to a `## ` heading before chunking — the manuals title
their sections with highlights, not with "Passo N:" patterns. On the Docling
path this promotion is inert by design: `chunk_document` chunks from
`dl_doc`, never from `Document.content`, and headings there come from layout
analysis. Chunk mapping is by page for Docling chunks; legacy chunks carry
no page (load_pdf joins pages before chunking), so every span attaches to
every chunk of the document — degraded but honest. TODO: bbox-level
span-to-chunk refinement.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from docquery.ingest.chunker import Chunk

logger = logging.getLogger(__name__)

#: Rects thinner than this are decoration — borders, separators — not marks
#: over text.
_MIN_WIDTH = 10
_MIN_HEIGHT = 5

#: A highlighted line whose glyphs run >= 1.3x the page's mean char size is a
#: title even when it is not full CAPS.
_LARGE_RATIO = 1.3


@dataclass(frozen=True)
class EmphasisSpan:
    text: str
    color: str  # "yellow" | "green"
    kind: str  # "rect" | "annotation"
    page: int  # 1-based
    bbox: tuple[float, float, float, float]  # (x0, top, x1, bottom)
    large: bool = False


def _color_name(color) -> str | None:
    """Map a pdfplumber color (gray, RGB or CMYK tuple) to yellow/green."""
    if color is None:
        return None
    values = (color,) if isinstance(color, (int, float)) else tuple(color)
    if len(values) == 1:
        r = g = b = float(values[0])
    elif len(values) == 3:
        r, g, b = (float(v) for v in values)
    elif len(values) == 4:
        c, m, y, k = (float(v) for v in values)
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return None
    if r > 0.8 and g > 0.8 and b < 0.4:
        return "yellow"
    if g > 0.6 and g > r + 0.2 and g > b + 0.2:
        return "green"
    return None


def _span_from_bbox(
    page, bbox, color: str, kind: str, number: int, mean_size: float
) -> EmphasisSpan | None:
    """Text under a mark, by geometric intersection (chars whose midpoint
    falls inside the cropped region). The ±1pt pad keeps glyphs sitting
    exactly on the rect edge — validated at 34/34 marks on the reference
    manual."""
    x0 = max(bbox[0] - 1, page.bbox[0])
    top = max(bbox[1] - 1, page.bbox[1])
    x1 = min(bbox[2] + 1, page.bbox[2])
    bottom = min(bbox[3] + 1, page.bbox[3])
    if x1 <= x0 or bottom <= top:
        return None
    region = page.crop((x0, top, x1, bottom))
    text = (region.extract_text() or "").strip()
    if not text:
        return None
    sizes = [c["size"] for c in region.chars]
    large = (
        bool(sizes)
        and mean_size > 0
        and (sum(sizes) / len(sizes) >= _LARGE_RATIO * mean_size)
    )
    return EmphasisSpan(
        text=text,
        color=color,
        kind=kind,
        page=number,
        bbox=(x0, top, x1, bottom),
        large=large,
    )


def _annotation_spans(page, number: int, mean_size: float) -> list[EmphasisSpan]:
    """/Highlight annotations (Acrobat, Google Docs): QuadPoints + color."""
    spans: list[EmphasisSpan] = []
    for annot in page.annots or []:
        try:
            data = annot.get("data") or {}
            if "Highlight" not in str(data.get("Subtype", "")):
                continue
            color = _color_name(data.get("C"))
            if color is None:
                continue
            quads = [float(v) for v in (data.get("QuadPoints") or [])]
            for i in range(0, len(quads) - 7, 8):
                xs = quads[i : i + 8 : 2]
                ys = quads[i + 1 : i + 8 : 2]
                # QuadPoints are bottom-origin; pdfplumber crops top-origin.
                bbox = (
                    min(xs),
                    page.height - max(ys),
                    max(xs),
                    page.height - min(ys),
                )
                span = _span_from_bbox(
                    page, bbox, color, "annotation", number, mean_size
                )
                if span:
                    spans.append(span)
        except Exception:
            logger.debug("Skipping unreadable annotation on page %d", number)
    return spans


def extract_emphasis(path: Path) -> dict[int, list[EmphasisSpan]]:
    """Yellow/green marks per page. A failing page logs and is skipped — a
    decoration must never fail an ingest (callers wrap the whole call too)."""
    import pdfplumber

    spans: dict[int, list[EmphasisSpan]] = {}
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            try:
                found: list[EmphasisSpan] = []
                sizes = [c["size"] for c in page.chars]
                mean_size = sum(sizes) / len(sizes) if sizes else 0.0
                for rect in page.rects:
                    if not rect.get("fill"):
                        continue
                    color = _color_name(rect.get("non_stroking_color"))
                    if color is None:
                        continue
                    if rect["width"] < _MIN_WIDTH or rect["height"] < _MIN_HEIGHT:
                        continue
                    span = _span_from_bbox(
                        page,
                        (rect["x0"], rect["top"], rect["x1"], rect["bottom"]),
                        color,
                        "rect",
                        number,
                        mean_size,
                    )
                    if span:
                        found.append(span)
                found.extend(_annotation_spans(page, number, mean_size))
                if found:
                    spans[number] = found
            except Exception:
                logger.warning(
                    "Emphasis extraction failed on page %d of %s; skipping the page",
                    number,
                    path,
                )
    return spans


def heading_candidates(spans: dict[int, list[EmphasisSpan]]) -> list[str]:
    """Yellow spans that read as titles: full-line CAPS, or notably large."""
    candidates: list[str] = []
    for page in spans.values():
        for span in page:
            if span.color != "yellow":
                continue
            has_letters = any(c.isalpha() for c in span.text)
            caps = has_letters and span.text == span.text.upper()
            if caps or span.large:
                candidates.append(span.text)
    return candidates


def promote_emphasis_headings(
    text: str, spans: dict[int, list[EmphasisSpan]]
) -> tuple[str, bool]:
    """Prefix `## ` to lines whose stripped text equals a heading candidate.

    Exact-line match, page-agnostic: the legacy loader already joined the
    pages, so page provenance is gone here, and an exact full-line collision
    across pages is negligible. Same contract as loader._promote_headings.
    """
    candidates = set(heading_candidates(spans))
    if not candidates:
        return text, False
    promoted = False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() in candidates:
            lines[i] = f"## {line}"
            promoted = True
    return "\n".join(lines), promoted


def attach_emphasis(doc, chunks: list[Chunk]) -> None:
    """Copy span texts into chunk metadata, by page when provenance exists.

    Docling chunks carry page_number; legacy chunks do not (load_pdf joins
    pages), so there every chunk gets every span — degraded on purpose, and
    the sparse injection still lets the document match its own marks.
    """
    spans = getattr(doc, "emphasis", None)
    if not spans:
        return
    paged = any(int(c.metadata.get("page_number", 0) or 0) > 0 for c in chunks)
    if paged:
        for chunk in chunks:
            page = int(chunk.metadata.get("page_number", 0) or 0)
            texts = [s.text for s in spans.get(page, [])]
            if texts:
                chunk.metadata["emphasis"] = texts
    else:
        texts = [s.text for page in sorted(spans) for s in spans[page]]
        if texts:
            for chunk in chunks:
                chunk.metadata["emphasis"] = list(texts)
