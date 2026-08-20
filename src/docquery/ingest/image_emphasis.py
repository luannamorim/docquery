"""Read the red boxes burned into a PDF's screenshots, as a signal.

The manuals mark which field or button a step refers to by drawing red
rectangles inside their screenshots. Those marks are raster — no `rg`
operator in the vector layer — so only image analysis sees them. Each
embedded image is scanned for red box outlines (HSV mask at both hue ends
of red, since red wraps around the hue axis), the inner crop is OCR'd with
the same RapidOCR engine Docling uses, and the recovered strings become
`emphasis_screen` chunk metadata plus lexical terms — never cited text.

Embedded images come from pypdf (`page.images`, the full-fidelity decoded
XObject) rather than from a page render, so resolution is the screenshot's
own. The OCR engine is built by Docling's `RapidOcrModel` — torch backend,
language policy from DOCLING_OCR_LANGS, weights from DOCLING_ARTIFACTS_PATH
when set (the Docker image prefetches them) — so this feature never
diverges from what page-level OCR would read.

Arrows and 1/2/3 numbering inside screenshots point at their targets rather
than enclosing them; only a VLM could resolve those, and they stay out of
scope until measurement shows the gap matters.
"""

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np

from docquery.config import Settings
from docquery.ingest.chunker import Chunk

logger = logging.getLogger(__name__)

#: Boxes smaller than this are specks or line fragments, not marks around a
#: field. Calibrated on the reference manual (correct boxes on p1, 3, 9, 10
#: and 14). Deliberately no fill-ratio filter: a thin outline has a high
#: bbox fill after the dilate, and solid arrows fall under this size floor.
_MIN_BOX_W = 30
_MIN_BOX_H = 12

#: Negative padding on the inner crop, so the red border itself never
#: reaches OCR, where it reads as noise glyphs.
_BORDER_PAD = 4


def find_red_boxes(image_rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of red outlines in an RGB image, as (x, y, w, h)."""
    import cv2

    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, (0, 90, 60), (12, 255, 255)) | cv2.inRange(
        hsv, (168, 90, 60), (180, 255, 255)
    )
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in contours]
    return [(x, y, w, h) for x, y, w, h in boxes if w >= _MIN_BOX_W and h >= _MIN_BOX_H]


@lru_cache(maxsize=1)
def _get_ocr(langs: tuple[str, ...], artifacts_path: str | None):
    """The RapidOCR engine exactly as Docling builds it.

    Reusing RapidOcrModel keeps model resolution — checkpoint choice per
    language, offline weights under artifacts_path — identical to the
    page-level OCR path, so the two can never disagree about what a
    character looks like.
    """
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.pipeline_options import RapidOcrOptions
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel

    model = RapidOcrModel(
        enabled=True,
        artifacts_path=Path(artifacts_path) if artifacts_path else None,
        options=RapidOcrOptions(backend="torch", lang=list(langs)),
        accelerator_options=AcceleratorOptions(),
    )
    return model.reader


def _ocr_crop(image_rgb: np.ndarray, box: tuple[int, int, int, int], engine) -> str:
    """OCR the inside of a box: border excluded, upscaled 2x for the small
    UI type screenshots are full of."""
    import cv2

    x, y, w, h = box
    inner_w, inner_h = w - 2 * _BORDER_PAD, h - 2 * _BORDER_PAD
    if inner_w <= 0 or inner_h <= 0:
        return ""
    crop = image_rgb[
        y + _BORDER_PAD : y + h - _BORDER_PAD, x + _BORDER_PAD : x + w - _BORDER_PAD
    ]
    crop = cv2.resize(crop, (inner_w * 2, inner_h * 2), interpolation=cv2.INTER_CUBIC)
    result = engine(crop)
    texts = list(getattr(result, "txts", None) or [])
    return " ".join(t.strip() for t in texts if t and t.strip())


def extract_screen_emphasis(path: Path, settings: Settings) -> dict[int, list[str]]:
    """OCR'd contents of red-boxed regions, per page. Never raises out of an
    ingest: a failing image logs and is skipped, like embedded_modified_at."""
    from pypdf import PdfReader

    engine = _get_ocr(
        tuple(settings.docling_ocr_langs),
        str(settings.docling_artifacts_path)
        if settings.docling_artifacts_path
        else None,
    )
    results: dict[int, list[str]] = {}
    reader = PdfReader(path)
    for number, page in enumerate(reader.pages, start=1):
        texts: list[str] = []
        for image in page.images:
            try:
                pil = image.image
                if pil is None:
                    continue
                rgb = np.asarray(pil.convert("RGB"))
                for box in find_red_boxes(rgb):
                    text = _ocr_crop(rgb, box, engine)
                    if text:
                        texts.append(text)
            except Exception:
                logger.warning(
                    "Screen-emphasis OCR failed on an image of page %d in %s",
                    number,
                    path,
                    exc_info=True,
                )
        if texts:
            results[number] = texts
    return results


def attach_screen_emphasis(doc, chunks: list[Chunk]) -> None:
    """Copy OCR'd box contents into chunk metadata, by page when possible.

    Same mapping rule as emphasis.attach_emphasis: Docling chunks carry
    page_number; legacy chunks do not, so every chunk gets everything.
    """
    found = getattr(doc, "emphasis_screen", None)
    if not found:
        return
    paged = any(int(c.metadata.get("page_number", 0) or 0) > 0 for c in chunks)
    if paged:
        for chunk in chunks:
            page = int(chunk.metadata.get("page_number", 0) or 0)
            texts = found.get(page, [])
            if texts:
                chunk.metadata["emphasis_screen"] = list(texts)
    else:
        texts = [t for page in sorted(found) for t in found[page]]
        if texts:
            for chunk in chunks:
                chunk.metadata["emphasis_screen"] = list(texts)
